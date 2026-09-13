#!/usr/bin/env bash
# Linux 호스트에서 진단 Python 본체를 준비하고 대화형으로 실행합니다.
set -euo pipefail

if [[ $(uname -s) != Linux ]]; then
  printf '%s\n' 'Linux 호스트에서 실행해 주세요.' >&2
  exit 1
fi
python_bin=''
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    python_bin=$candidate
    break
  fi
done
if [[ -z $python_bin ]]; then
  printf '%s\n' 'Python 3.8 이상이 필요합니다. 호스트에 준비한 후 다시 실행하세요. 자동 설치하지 않습니다.' >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' 'Docker 명령을 찾지 못했습니다. Docker가 설치된 호스트에서 실행해 주세요.' >&2
  exit 1
fi

# 내려받은 실행기는 같은 폴더의 본체를 우선 사용합니다.
if [[ -n ${BASH_SOURCE[0]:-} ]]; then
  script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
  if [[ -f $script_dir/mate_diagnose.py ]]; then
    exec "$python_bin" -B "$script_dir/mate_diagnose.py" "$@"
  fi
fi
if ! command -v curl >/dev/null 2>&1; then
  printf '%s\n' '진단 파일 다운로드에 curl이 필요합니다.' >&2
  exit 1
fi
umask 077
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
printf '%s\n' '공식 저장소에서 진단 스크립트를 내려받습니다. 서비스 설정은 변경하지 않습니다.'
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 10 --max-time 60 \
  'https://raw.githubusercontent.com/colaiuta77/bookoasis_mate/main/scripts/mate_diagnose.py' \
  --output "$temporary/mate_diagnose.py"
"$python_bin" -B "$temporary/mate_diagnose.py" "$@"
