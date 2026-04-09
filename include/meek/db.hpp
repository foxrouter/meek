// include/meek/db.hpp — SQLite3 RAII wrapper with WAL mode and fixed schema.
//
// Database uses prepared statements for all writes to avoid SQL injection and
// reduce per-row overhead.  The schema is created on first open by
// apply_schema() using CREATE TABLE IF NOT EXISTS statements.
//
// All public methods are called from the output thread only.

#pragma once

#include <sqlite3.h>

#include <cstdint>
#include <iostream>
#include <memory>
#include <string>

namespace meek {

// ---------------------------------------------------------------------------
// RAII statement handle
// ---------------------------------------------------------------------------

struct StmtDeleter {
  void operator()(sqlite3_stmt* s) noexcept {
    if (s)
      sqlite3_finalize(s);
  }
};
using StmtPtr = std::unique_ptr<sqlite3_stmt, StmtDeleter>;

// ---------------------------------------------------------------------------
// Database — owns the SQLite connection and all prepared statements
// ---------------------------------------------------------------------------

class Database {
 public:
  /// Open (or create) the database at path.
  /// Returns nullptr + logs on failure.
  [[nodiscard]] static std::unique_ptr<Database> open(const std::string& path);

  ~Database();

  Database(const Database&) = delete;
  Database& operator=(const Database&) = delete;

  /// Insert a signal observation and return its row-id, or -1 on error.
  [[nodiscard]] std::int64_t insert_signal(const std::string& source, const std::string& notes,
                                           std::int64_t timestamp_ns, double center_freq_hz);

  /// Upsert the modulation classifier method and return its row-id, or -1.
  [[nodiscard]] std::int64_t upsert_method(const std::string& name, const std::string& params_json);

  /// Insert a classification example row.  Returns 0 on success, -1 on error.
  int insert_example(std::int64_t signal_id, std::int64_t method_id, float confidence,
                     const std::string& notes, const std::string& result);

 private:
  explicit Database(sqlite3* db) : db_(db) {}

  [[nodiscard]] bool apply_schema();
  [[nodiscard]] bool prepare_statements();

  sqlite3* db_{nullptr};
  StmtPtr insert_signal_stmt_;
  StmtPtr insert_method_stmt_;
  StmtPtr select_method_stmt_;
  StmtPtr insert_example_stmt_;
};

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

inline std::unique_ptr<Database> Database::open(const std::string& path) {
  sqlite3* raw = nullptr;
  if (sqlite3_open(path.c_str(), &raw) != SQLITE_OK) {
    const char* msg = raw ? sqlite3_errmsg(raw) : "out of memory";
    std::cerr << "[DB] sqlite3_open(" << path << "): " << msg << "\n";
    if (raw) {
      sqlite3_close(raw);
    }
    return nullptr;
  }
  auto db = std::unique_ptr<Database>(new Database(raw));
  sqlite3_exec(raw, "PRAGMA foreign_keys = ON;", nullptr, nullptr, nullptr);
  // WAL mode: reduces write-contention when readers and writer coexist.
  // The callback captures the journal mode returned by SQLite so we can
  // confirm WAL was actually applied (SQLITE_OK alone is not sufficient —
  // some filesystems silently ignore the request and stay in delete mode).
  {
    char* wal_err = nullptr;
    std::string actual_mode;
    auto mode_cb = [](void* ctx, int argc, char** argv, char**) -> int {
      if (argc > 0 && argv[0] && argv[0][0] != '\0')
        *static_cast<std::string*>(ctx) = argv[0];
      return 0;
    };
    const int wal_rc =
        sqlite3_exec(raw, "PRAGMA journal_mode = WAL;", mode_cb, &actual_mode, &wal_err);
    if (wal_rc != SQLITE_OK) {
      std::cerr << "[DB] journal_mode WAL failed: " << (wal_err ? wal_err : sqlite3_errmsg(raw))
                << " - using default journal mode\n";
      sqlite3_free(wal_err);
    } else if (actual_mode != "wal") {
      std::cerr << "[DB] journal_mode WAL not available (continuing with journal_mode='"
                << actual_mode << "')\n";
    }
  }
  sqlite3_exec(raw, "PRAGMA synchronous = NORMAL;", nullptr, nullptr, nullptr);
  // Retry SQLITE_BUSY for up to 5 seconds before propagating the error.
  // Required in WAL mode when decode_candidates.py or admin tools hold read
  // transactions concurrently with the daemon's write path.
  {
    const int bt_rc = sqlite3_busy_timeout(raw, 5000);
    if (bt_rc != SQLITE_OK) {
      std::cerr << "[DB] sqlite3_busy_timeout failed: " << sqlite3_errstr(bt_rc) << " (rc=" << bt_rc
                << ") - SQLITE_BUSY errors may reach callers immediately\n";
    }
  }
  // Increase page cache to reduce write stalls under burst classification.
  // Negative value is in KiB: -8000 = 8000 KiB (~8 MiB).
  {
    char* cache_err = nullptr;
    const int cache_rc =
        sqlite3_exec(raw, "PRAGMA cache_size = -8000;", nullptr, nullptr, &cache_err);
    if (cache_rc != SQLITE_OK) {
      std::cerr << "[DB] cache_size pragma failed: "
                << (cache_err ? cache_err : sqlite3_errmsg(raw)) << " - using default page cache\n";
      sqlite3_free(cache_err);
    }
  }

  if (!db->apply_schema() || !db->prepare_statements()) {
    return nullptr;
  }
  return db;
}

inline Database::~Database() {
  insert_signal_stmt_.reset();
  insert_method_stmt_.reset();
  select_method_stmt_.reset();
  insert_example_stmt_.reset();
  if (db_) {
    sqlite3_close(db_);
    db_ = nullptr;
  }
}

inline bool Database::apply_schema() {
  // Schema version 1 — baseline
  static constexpr const char* kSchema = R"sql(
    CREATE TABLE IF NOT EXISTS signals (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp    TEXT NOT NULL DEFAULT (datetime('now')),
      source       TEXT,
      notes        TEXT,
      timestamp_ns     INTEGER,
      center_freq_hz   REAL
    );
    CREATE TABLE IF NOT EXISTS methods (
      id     INTEGER PRIMARY KEY AUTOINCREMENT,
      name   TEXT UNIQUE NOT NULL,
      params TEXT
    );
    CREATE TABLE IF NOT EXISTS examples (
      id         INTEGER PRIMARY KEY AUTOINCREMENT,
      signal_id  INTEGER NOT NULL REFERENCES signals(id),
      method_id  INTEGER NOT NULL REFERENCES methods(id),
      result     TEXT,
      confidence REAL,
      notes      TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
  )sql";
  char* err = nullptr;
  if (sqlite3_exec(db_, kSchema, nullptr, nullptr, &err) != SQLITE_OK) {
    std::cerr << "[DB] apply_schema: " << (err ? err : sqlite3_errmsg(db_)) << "\n";
    sqlite3_free(err);
    return false;
  }

  // Indexes — applied after schema creation; idempotent via IF NOT EXISTS.
  // These cover every join pattern used by decode_candidates.py and
  // docs/db_queries.sql. Wrapped in an explicit transaction so all four
  // indexes are built atomically and SQLite avoids a separate autocommit
  // fsync per statement (faster on existing DBs with many rows).
  static constexpr const char* kIndexes = R"sql(
    BEGIN;
    CREATE INDEX IF NOT EXISTS idx_examples_signal_id
      ON examples(signal_id);
    CREATE INDEX IF NOT EXISTS idx_examples_method_id
      ON examples(method_id);
    CREATE INDEX IF NOT EXISTS idx_examples_confidence
      ON examples(confidence DESC)
      WHERE confidence IS NOT NULL;
    CREATE INDEX IF NOT EXISTS idx_signals_timestamp
      ON signals(timestamp DESC);
    COMMIT;
  )sql";
  {
    char* idx_err = nullptr;
    if (sqlite3_exec(db_, kIndexes, nullptr, nullptr, &idx_err) != SQLITE_OK) {
      std::cerr << "[DB] apply indexes: " << (idx_err ? idx_err : sqlite3_errmsg(db_)) << "\n";
      sqlite3_free(idx_err);
      // Roll back any partial transaction so the connection is not left with
      // schema locks held before migrations and prepare_statements() run.
      // Guard with sqlite3_get_autocommit: ROLLBACK outside a transaction
      // returns an error ("cannot rollback - no transaction is active"), so
      // only issue it when we are actually inside one (autocommit == 0).
      if (sqlite3_get_autocommit(db_) == 0) {
        sqlite3_exec(db_, "ROLLBACK;", nullptr, nullptr, nullptr);
      }
      // Non-fatal: the daemon continues; missing indexes only degrade performance.
    }
  }

  // Migration: add notes column to signals if it does not exist yet.
  // ALTER TABLE ... ADD COLUMN returns SQLITE_ERROR when the column already
  // exists; that is expected and harmless.  Log any other error for diagnostics
  // but do not abort — statements will still fail in prepare_statements() if
  // the column is genuinely absent.
  {
    char* mig_err = nullptr;
    const int mig_rc =
        sqlite3_exec(db_, "ALTER TABLE signals ADD COLUMN notes TEXT;", nullptr, nullptr, &mig_err);
    if (mig_rc != SQLITE_OK) {
      const std::string msg = mig_err ? mig_err : sqlite3_errmsg(db_);
      sqlite3_free(mig_err);
      // "duplicate column name" is the expected error when the column already
      // exists; silence it to avoid noisy logs on every clean-install startup.
      if (msg.find("duplicate column") == std::string::npos) {
        std::cerr << "[DB] migrate signals.notes: " << msg << "\n";
      }
    }
  }

  // Migration: add params column to methods if it does not exist yet.
  // Databases created before this column was added will fail to prepare the
  // insert_method statement unless the column is added here at startup.
  {
    char* mig_err = nullptr;
    const int mig_rc = sqlite3_exec(db_, "ALTER TABLE methods ADD COLUMN params TEXT;", nullptr,
                                    nullptr, &mig_err);
    if (mig_rc != SQLITE_OK) {
      const std::string msg = mig_err ? mig_err : sqlite3_errmsg(db_);
      sqlite3_free(mig_err);
      if (msg.find("duplicate column") == std::string::npos) {
        std::cerr << "[DB] migrate methods.params: " << msg << "\n";
      }
    }
  }

  // Migration: add timestamp_ns column to signals if it does not exist yet.
  // Enables sub-second timestamp resolution for burst classification events.
  {
    char* mig_err = nullptr;
    const int mig_rc = sqlite3_exec(db_, "ALTER TABLE signals ADD COLUMN timestamp_ns INTEGER;",
                                    nullptr, nullptr, &mig_err);
    if (mig_rc != SQLITE_OK) {
      const std::string msg = mig_err ? mig_err : sqlite3_errmsg(db_);
      sqlite3_free(mig_err);
      if (msg.find("duplicate column") == std::string::npos) {
        std::cerr << "[DB] migrate signals.timestamp_ns: " << msg << "\n";
      }
    }
  }

  // idx_signals_timestamp_ns is created here (after the migration that adds
  // the column) so it never fails with "no such column" on an upgraded DB.
  {
    char* idx_err = nullptr;
    const int idx_rc = sqlite3_exec(db_,
                                    "CREATE INDEX IF NOT EXISTS idx_signals_timestamp_ns "
                                    "ON signals(timestamp_ns DESC) WHERE timestamp_ns IS NOT NULL;",
                                    nullptr, nullptr, &idx_err);
    if (idx_rc != SQLITE_OK) {
      std::cerr << "[DB] idx_signals_timestamp_ns: " << (idx_err ? idx_err : sqlite3_errmsg(db_))
                << "\n";
      sqlite3_free(idx_err);
      // Non-fatal: missing index only degrades time-range query performance.
    }
  }

  // Migration: add result column to examples if it does not exist yet.
  // decode_candidates.py selects e.result; databases created before this
  // column was added must be upgraded here so the SELECT does not fail.
  {
    char* mig_err = nullptr;
    const int mig_rc = sqlite3_exec(db_, "ALTER TABLE examples ADD COLUMN result TEXT;", nullptr,
                                    nullptr, &mig_err);
    if (mig_rc != SQLITE_OK) {
      const std::string msg = mig_err ? mig_err : sqlite3_errmsg(db_);
      sqlite3_free(mig_err);
      if (msg.find("duplicate column") == std::string::npos) {
        std::cerr << "[DB] migrate examples.result: " << msg << "\n";
      }
    }
  }

  // Migration: add center_freq_hz column to signals if it does not exist yet.
  // Enables queries filtered by capture frequency; existing rows get NULL.
  {
    char* mig_err = nullptr;
    const int mig_rc = sqlite3_exec(db_, "ALTER TABLE signals ADD COLUMN center_freq_hz REAL;",
                                    nullptr, nullptr, &mig_err);
    if (mig_rc != SQLITE_OK) {
      const std::string msg = mig_err ? mig_err : sqlite3_errmsg(db_);
      sqlite3_free(mig_err);
      if (msg.find("duplicate column") == std::string::npos) {
        std::cerr << "[DB] migrate signals.center_freq_hz: " << msg << "\n";
      }
    }
  }

  // Index on center_freq_hz — created after the migration that adds the column.
  {
    char* idx_err = nullptr;
    const int idx_rc =
        sqlite3_exec(db_,
                     "CREATE INDEX IF NOT EXISTS idx_signals_center_freq_hz "
                     "ON signals(center_freq_hz) WHERE center_freq_hz IS NOT NULL;",
                     nullptr, nullptr, &idx_err);
    if (idx_rc != SQLITE_OK) {
      std::cerr << "[DB] idx_signals_center_freq_hz: " << (idx_err ? idx_err : sqlite3_errmsg(db_))
                << "\n";
      sqlite3_free(idx_err);
      // Non-fatal: missing index only degrades frequency-range query performance.
    }
  }

  return true;
}

inline bool Database::prepare_statements() {
  sqlite3_stmt* s = nullptr;

  static constexpr const char* kInsertSignal =
      "INSERT INTO signals(source, notes, timestamp_ns, center_freq_hz) VALUES(?, ?, ?, ?)";
  if (sqlite3_prepare_v2(db_, kInsertSignal, -1, &s, nullptr) != SQLITE_OK) {
    std::cerr << "[DB] prepare insert_signal: " << sqlite3_errmsg(db_) << "\n";
    return false;
  }
  insert_signal_stmt_.reset(s);

  static constexpr const char* kInsertMethod =
      "INSERT OR IGNORE INTO methods(name, params) VALUES(?, ?)";
  if (sqlite3_prepare_v2(db_, kInsertMethod, -1, &s, nullptr) != SQLITE_OK) {
    std::cerr << "[DB] prepare insert_method: " << sqlite3_errmsg(db_) << "\n";
    return false;
  }
  insert_method_stmt_.reset(s);

  static constexpr const char* kInsertExample =
      "INSERT INTO examples(signal_id, method_id, confidence, notes, result) "
      "VALUES(?, ?, ?, ?, ?)";
  if (sqlite3_prepare_v2(db_, kInsertExample, -1, &s, nullptr) != SQLITE_OK) {
    std::cerr << "[DB] prepare insert_example: " << sqlite3_errmsg(db_) << "\n";
    return false;
  }
  insert_example_stmt_.reset(s);

  static constexpr const char* kSelectMethod = "SELECT id FROM methods WHERE name = ? LIMIT 1";
  if (sqlite3_prepare_v2(db_, kSelectMethod, -1, &s, nullptr) != SQLITE_OK) {
    std::cerr << "[DB] prepare select_method: " << sqlite3_errmsg(db_) << "\n";
    return false;
  }
  select_method_stmt_.reset(s);

  return true;
}

inline std::int64_t Database::insert_signal(const std::string& source, const std::string& notes,
                                            std::int64_t timestamp_ns, double center_freq_hz) {
  // timestamp_ns: epoch nanoseconds when available; otherwise a monotonic
  // steady_clock-based fallback for pre-epoch/nonrepresentable wall-clock
  // timestamps. 0 means only "unset/unknown". INT64_MAX = year 2262.
  sqlite3_stmt* stmt = insert_signal_stmt_.get();
  sqlite3_reset(stmt);
  sqlite3_bind_text(stmt, 1, source.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(stmt, 2, notes.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_int64(stmt, 3, timestamp_ns);
  sqlite3_bind_double(stmt, 4, center_freq_hz);
  if (sqlite3_step(stmt) != SQLITE_DONE)
    return -1;
  return sqlite3_last_insert_rowid(db_);
}

inline std::int64_t Database::upsert_method(const std::string& name,
                                            const std::string& params_json) {
  sqlite3_stmt* stmt = insert_method_stmt_.get();
  sqlite3_reset(stmt);
  sqlite3_bind_text(stmt, 1, name.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(stmt, 2, params_json.c_str(), -1, SQLITE_TRANSIENT);
  const int rc = sqlite3_step(stmt);
  if (rc != SQLITE_DONE && rc != SQLITE_CONSTRAINT) {
    // SQLITE_CONSTRAINT is expected when the row already exists (IGNORE).
    std::cerr << "[DB] upsert_method failed: " << sqlite3_errmsg(db_) << "\n";
    return -1;
  }

  // Retrieve the row-id using the cached prepared statement.
  // (INSERT OR IGNORE won't change last_insert_rowid if the row already existed.)
  sqlite3_stmt* sel = select_method_stmt_.get();
  sqlite3_reset(sel);
  sqlite3_bind_text(sel, 1, name.c_str(), -1, SQLITE_TRANSIENT);
  std::int64_t id = -1;
  if (sqlite3_step(sel) == SQLITE_ROW)
    id = sqlite3_column_int64(sel, 0);
  return id;
}

inline int Database::insert_example(std::int64_t signal_id, std::int64_t method_id,
                                    float confidence, const std::string& notes,
                                    const std::string& result) {
  sqlite3_stmt* stmt = insert_example_stmt_.get();
  sqlite3_reset(stmt);
  sqlite3_bind_int64(stmt, 1, signal_id);
  sqlite3_bind_int64(stmt, 2, method_id);
  sqlite3_bind_double(stmt, 3, static_cast<double>(confidence));
  sqlite3_bind_text(stmt, 4, notes.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(stmt, 5, result.c_str(), -1, SQLITE_TRANSIENT);
  return (sqlite3_step(stmt) == SQLITE_DONE) ? 0 : -1;
}

}  // namespace meek
