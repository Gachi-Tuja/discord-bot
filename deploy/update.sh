#!/usr/bin/env bash
# GitHub 최신 릴리스로 서버를 맞춘다. ipobot-update.timer가 주기적으로 실행한다.
#
# 1. 최신 릴리스 태그가 지금 체크아웃된 태그와 같으면 아무것도 하지 않는다.
# 2. 추적 파일에 커밋 안 된 수정이 있으면 손대지 않고 실패한다.
# 3. 태그를 체크아웃하고 `uv sync --frozen` 후 테스트를 돌린다.
# 4. 봇을 재시작해 active인지 확인한다.
# 3·4가 실패하면 이전 커밋으로 되돌리고 다시 재시작한다.
#
# 체크아웃이 이 파일 자신을 바꾸므로 본문 전체를 함수로 읽은 뒤 실행한다.
set -euo pipefail

main() {
	local repo_dir="${REPO_DIR:-$HOME/ipobot}"
	local repo="${GITHUB_REPO:-Gachi-Tuja/discord-bot}"
	local service="${SERVICE:-ipobot}"
	local uv="${UV:-$HOME/.local/bin/uv}"

	cd "$repo_dir"
	exec 9>"$repo_dir/.update.lock"
	flock -n 9 || { echo "다른 업데이트가 실행 중"; return 0; }

	local latest current
	latest=$(curl -fsS "https://api.github.com/repos/$repo/releases/latest" |
		python3 -c 'import json, sys; print(json.load(sys.stdin)["tag_name"])')
	current=$(git describe --tags --exact-match 2>/dev/null || true)
	[[ "$latest" == "$current" ]] && return 0

	if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
		echo "추적 파일에 커밋 안 된 수정이 있어 $latest 배포를 건너뜀" >&2
		git status --short --untracked-files=no >&2
		return 1
	fi

	local previous
	previous=$(git rev-parse HEAD)
	echo "배포: ${current:-$previous} -> $latest"
	git fetch --quiet --tags --force origin
	switch_to "refs/tags/$latest" "$uv"

	if ! "$uv" run --frozen pytest -q -p no:cacheprovider; then
		rollback "$previous" "$uv" "$service" "테스트 실패"
		return 1
	fi
	if ! restart "$service"; then
		rollback "$previous" "$uv" "$service" "재시작 후 active 아님"
		return 1
	fi
	echo "배포 완료: $latest"
}

switch_to() {
	git -c advice.detachedHead=false checkout --quiet "$1"
	"$2" sync --frozen --quiet
}

restart() {
	sudo systemctl restart "$1"
	sleep 10
	systemctl is-active --quiet "$1"
}

rollback() {
	echo "$4: $1(으)로 되돌림" >&2
	switch_to "$1" "$2"
	restart "$3" || echo "되돌린 버전도 active 아님" >&2
}

main "$@"
exit
