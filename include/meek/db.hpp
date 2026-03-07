// include/meek/db.hpp — SQLite3 RAII wrapper with WAL mode and fixed schema.
//
// Database uses prepared statements for all writes to avoid SQL injection and
// reduce per-row overhead.  The schema is created on first open by
// apply_schema() using CREATE TABLE IF NOT EXISTS statements.
//
// All public methods are called from the output thread only.

#pragma once

#include <cstdint>
#include <iostream>
#include <memory>
#include <string>

#include <sqlite3.h>

namespace meek {

// ---------------------------------------------------------------------------
// RAII statement handle
// ---------------------------------------------------------------------------

struct StmtDeleter {
  void operator()(sqlite3_stmt* s) noexcept {
    if (s) sqlite3_finalize(s);
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
  [[nodiscard]] std::int64_t insert_signal(const std::string& source,
                                           const std::string& notes);

  /// Upsert the modulation classifier method and return its row-id, or -1.
  [[nodiscard]] std::int64_t upsert_method(const std::string& name,
                                           const std::string& params_json);

  /// Insert a classification example row.  Returns 0 on success, -1 on error.
  int insert_example(std::int64_t signal_id, std::int64_t method_id,
                     float confidence, const std::string& notes);

 private:
  explicit Database(sqlite3* db) : db_(db) {}

  [[nodiscard]] bool apply_schema();
  [[nodiscard]] bool prepare_statements();

  sqlite3* db_{nullptr};
  StmtPtr insert_signal_stmt_;
  StmtPtr insert_method_stmt_;
  StmtPtr insert_example_stmt_;
};

// ---------------------------------------------------------------------------
// Implementation
// ---------------------------------------------------------------------------

inline std::unique_ptr<Database> Database::open(const std::string& path) {
  sqlite3* raw = nullptr;
  if (sqlite3_open(path.c_str(), &raw) != SQLITE_OK) {
    if (raw) {
      sqlite3_close(raw);
    }
    return nullptr;
  }
  auto db = std::unique_ptr<Database>(new Database(raw));
  sqlite3_exec(raw, "PRAGMA foreign_keys = ON;", nullptr, nullptr, nullptr);
  // WAL mode: reduces write-contention when readers and writer coexist.
  sqlite3_exec(raw, "PRAGMA journal_mode = WAL;", nullptr, nullptr, nullptr);
  sqlite3_exec(raw, "PRAGMA synchronous = NORMAL;", nullptr, nullptr, nullptr);

  if (!db->apply_schema() || !db->prepare_statements()) {
    return nullptr;
  }
  return db;
}

inline Database::~Database() {
  insert_signal_stmt_.reset();
  insert_method_stmt_.reset();
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
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      timestamp TEXT NOT NULL DEFAULT (datetime('now')),
      source    TEXT,
      notes     TEXT
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
      confidence REAL,
      notes      TEXT,
      created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
  )sql";
  char* err = nullptr;
  if (sqlite3_exec(db_, kSchema, nullptr, nullptr, &err) != SQLITE_OK) {
    sqlite3_free(err);
    return false;
  }
  return true;
}

inline bool Database::prepare_statements() {
  sqlite3_stmt* s = nullptr;

  static constexpr const char* kInsertSignal =
      "INSERT INTO signals(source, notes) VALUES(?, ?)";
  if (sqlite3_prepare_v2(db_, kInsertSignal, -1, &s, nullptr) != SQLITE_OK) {
    return false;
  }
  insert_signal_stmt_.reset(s);

  static constexpr const char* kInsertMethod =
      "INSERT OR IGNORE INTO methods(name, params) VALUES(?, ?)";
  if (sqlite3_prepare_v2(db_, kInsertMethod, -1, &s, nullptr) != SQLITE_OK) {
    return false;
  }
  insert_method_stmt_.reset(s);

  static constexpr const char* kInsertExample =
      "INSERT INTO examples(signal_id, method_id, confidence, notes) "
      "VALUES(?, ?, ?, ?)";
  if (sqlite3_prepare_v2(db_, kInsertExample, -1, &s, nullptr) != SQLITE_OK) {
    return false;
  }
  insert_example_stmt_.reset(s);

  return true;
}

inline std::int64_t Database::insert_signal(const std::string& source,
                                             const std::string& notes) {
  sqlite3_stmt* stmt = insert_signal_stmt_.get();
  sqlite3_reset(stmt);
  sqlite3_bind_text(stmt, 1, source.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(stmt, 2, notes.c_str(), -1, SQLITE_TRANSIENT);
  if (sqlite3_step(stmt) != SQLITE_DONE) return -1;
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

  // Retrieve the row-id (INSERT OR IGNORE won't change last_insert_rowid if
  // the row already existed).
  sqlite3_stmt* sel = nullptr;
  static constexpr const char* kSel =
      "SELECT id FROM methods WHERE name = ? LIMIT 1";
  if (sqlite3_prepare_v2(db_, kSel, -1, &sel, nullptr) != SQLITE_OK)
    return -1;
  sqlite3_bind_text(sel, 1, name.c_str(), -1, SQLITE_TRANSIENT);
  std::int64_t id = -1;
  if (sqlite3_step(sel) == SQLITE_ROW) id = sqlite3_column_int64(sel, 0);
  sqlite3_finalize(sel);
  return id;
}

inline int Database::insert_example(std::int64_t signal_id,
                                     std::int64_t method_id, float confidence,
                                     const std::string& notes) {
  sqlite3_stmt* stmt = insert_example_stmt_.get();
  sqlite3_reset(stmt);
  sqlite3_bind_int64(stmt, 1, signal_id);
  sqlite3_bind_int64(stmt, 2, method_id);
  sqlite3_bind_double(stmt, 3, static_cast<double>(confidence));
  sqlite3_bind_text(stmt, 4, notes.c_str(), -1, SQLITE_TRANSIENT);
  return (sqlite3_step(stmt) == SQLITE_DONE) ? 0 : -1;
}

}  // namespace meek
