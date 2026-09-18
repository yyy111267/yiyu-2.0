#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo_dir="$(git -C "$project_dir" rev-parse --show-toplevel)"
deploy_host="${DEPLOY_HOST:-ubuntu@49.233.197.83}"
deploy_dir="${DEPLOY_DIR:-/home/ubuntu/yiyu-repo}"
deploy_url="${DEPLOY_URL:-https://learntofish.fun/ready}"
release_sha="$(git -C "$repo_dir" rev-parse HEAD)"

if [[ -n "$(git -C "$repo_dir" status --porcelain)" ]]; then
  echo "发布中止：本地工作区不干净，请先提交全部修改。" >&2
  exit 2
fi

if [[ "$(git -C "$repo_dir" branch --show-current)" != "main" ]]; then
  echo "发布中止：只能从 main 分支发布。" >&2
  exit 2
fi

echo "▶ 发布前安全门禁"
"$project_dir/.venv/bin/python" -m evaluation.e2e.scripts.run_security_gate

git -C "$repo_dir" fetch origin main
if [[ "$(git -C "$repo_dir" rev-parse origin/main)" != "$release_sha" ]]; then
  echo "发布中止：本地 HEAD 尚未与 origin/main 同步。" >&2
  exit 2
fi

echo "▶ 部署 $release_sha 到 $deploy_host"
bundle_file="$(mktemp -t yiyu-release.XXXXXX.bundle)"
remote_bundle="/tmp/yiyu-release-${release_sha}.bundle"
trap 'rm -f "$bundle_file"' EXIT
git -C "$repo_dir" bundle create "$bundle_file" main
scp -q -o BatchMode=yes "$bundle_file" "$deploy_host:$remote_bundle"

ssh -o BatchMode=yes "$deploy_host" bash -s -- "$deploy_dir" "$release_sha" "$remote_bundle" <<'REMOTE'
set -euo pipefail
deploy_dir="$1"
release_sha="$2"
remote_bundle="$3"
cd "$deploy_dir"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "发布中止：生产仓库存在未提交修改。" >&2
  exit 3
fi

previous_sha="$(git rev-parse HEAD)"
git bundle verify "$remote_bundle"
git fetch "$remote_bundle" refs/heads/main:refs/remotes/release/main
rm -f "$remote_bundle"
if [[ "$(git rev-parse refs/remotes/release/main)" != "$release_sha" ]]; then
  echo "发布中止：发布包与指定提交不一致。" >&2
  exit 3
fi

rollback() {
  echo "健康检查失败，回滚到 $previous_sha" >&2
  git reset --hard "$previous_sha"
  cd yiyu-agent
  sudo -n docker compose build api
  sudo -n docker compose up -d api
}
trap rollback ERR

git merge --ff-only "$release_sha"
cd yiyu-agent
sudo -n docker compose build api
sudo -n docker compose up -d api

for attempt in {1..24}; do
  if curl -fsS --max-time 5 http://127.0.0.1:8080/ready >/tmp/yiyu-ready.json; then
    cat /tmp/yiyu-ready.json
    echo
    trap - ERR
    exit 0
  fi
  sleep 5
done
false
REMOTE

echo "▶ 公网健康检查"
curl -fsS --max-time 15 "$deploy_url"
echo
echo "✅ 生产发布完成：$release_sha"
