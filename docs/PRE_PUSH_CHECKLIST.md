# 推送前检查清单

每次 `git push` 前按此清单逐条核对，避免泄漏密钥/敏感文件/未完成改动。

## 必查（30 秒）

### 1. 工作树状态干净
```bash
git status --short
```
- 应该只有意图推送的改动
- 不应有 `.env`、`*.key`、`*.pem` 等敏感文件
- 不应有 `outputs/`、`__pycache__/` 等生成物（检查 `.gitignore`）

### 2. 密钥扫描
```bash
# 扫常见密钥前缀
git grep -nE "sk-[A-Za-z0-9_-]{20,}|sk-ant-|ghp_|github_pat_|xox[abp]-|AIza[0-9A-Za-z_-]{35}" -- ':!*.md'

# 扫常见配置模式
git grep -nE "(api[_-]?key|secret|token|password|private[_-]?key)\s*[=:]\s*['\"][^'\"]{16,}" -- ':!*.md'
```
- 应 0 命中
- 命中 → 立即 `bash /f/C/tools/redact-secrets/redact.sh` 清理

### 3. 历史扫描（防止密钥藏在历史里）
```bash
# 全历史 grep（包括已删除的 commit）
git log --all -p | grep -E "sk-cp|sk-api-umWJ" | head -3
```
- 应 0 命中
- 命中 → 同上清理脚本

### 4. .gitignore 覆盖
```bash
cat .gitignore
```
- 应包含：`.env`、`__pycache__/`、`*.pyc`、`outputs/`、`.venv/`、`node_modules/`、`*.key`、`*.pem`
- 不在 → 补上

### 5. 远程同步状态
```bash
git fetch origin
git status -sb
```
- 看 `ahead/behind` 计数
- `behind` → 不要强推，先 `git pull --rebase`
- `ahead` 且远程有未 review 的 commit → `git push --force-with-lease` 而非 `--force`

### 6. 测试基线
```bash
pytest tests/ -q
```
- 全过再推

## 推荐（额外 1 分钟）

### 7. 验证 commit message
```bash
git log --oneline -5
```
- 应描述清楚改动（不像 `wip`/`fix`/`tmp`）

### 8. 验证 commit 作者
```bash
git log -1 --format='%an <%ae>'
```
- 应是你自己的邮箱（不是某个 CI bot 误设）

### 9. SSH / PAT 状态
```bash
ssh -T git@github.com  # 或 git push 前的认证测试
```

## 一键脚本（可选）

把以上 1-4 步串起来：

```bash
# /f/C/tools/redact-secrets/pre-push-check.sh
git status --short && echo "---"
git grep -nE "sk-[A-Za-z0-9_-]{20,}" -- ':!*.md' | head -5 && echo "---"
git log --all -p 2>/dev/null | grep -E "sk-cp|sk-api-umWJ" | head -3 && echo "---"
cat .gitignore
```

## 紧急回滚（推错了）

如果**密钥已推上去**：
1. **立即**到对应服务（MiniMax/GitHub/OpenAI 等）撤销该 key
2. 重新生成 key
3. `git filter-repo` 清理历史（用 `/f/C/tools/redact-secrets/redact.sh`）
4. `git push --force-with-lease`（告知协作者重新 clone）

如果**普通代码推错了**：
```bash
git revert HEAD             # 软回滚（保留历史）
git push                    # 推送 revert commit
# 或
git reset --hard HEAD~1     # 硬回滚（修改历史）
git push --force-with-lease  # 强制推（仅个人仓库）
```

## 历史教训

- 2026-08-20：本地 git history 残留 2 个 MiniMax key（fbe7f75 + d8e83d2 前的 commits）。
  - 远程未推，无实际泄漏。
  - `git filter-branch` 在 Windows 下损坏 .git/objects；改用 `git-filter-repo`。
  - 现已重建为单一 commit `b0715c9 rebuild: ...`。
- 见 `/f/C/tools/redact-secrets/redact.sh` 与 `expressions.txt`。