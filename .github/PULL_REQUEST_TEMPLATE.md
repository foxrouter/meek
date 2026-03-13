## Description

<!-- Describe what this PR changes and why. -->

## Checklist

- [ ] `clang-format -i src/*.cpp include/meek/*.hpp tools/*.cpp` run and no diff remains
- [ ] `clang-tidy` passes with no new `error:` findings
- [ ] All CI jobs pass (Python tests, C++ build, sanitizer, security scan)
- [ ] New or changed environment variables documented in `docs/rf-adapt-intel-plan.md`
- [ ] systemd unit / drop-in files updated if deployment behaviour changed
- [ ] `.github/copilot-instructions.md` / `docs/` updated if architecture or ops procedures changed
- [ ] No secrets or `.env` / `config/thresholds.env` content committed
