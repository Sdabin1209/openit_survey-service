#!/usr/bin/env python3
"""DatabaseDeclaration(db/database.json)을 계약 스키마로 검증하고 초기화 스크립트가 읽는 탭 구분 표(이름, 데이터베이스, 권한)로 바꾼다.
왜 표인가: 공식 postgres 이미지에는 파이썬·jq가 없어 초기화 스크립트(sh)가 JSON을 읽을 수 없다. 변환은 빌드 호스트에서 한 번 하고 이미지에는 표와 선언(라벨)이 같이 들어간다.
사용: gen_accounts.py <database.json> [<accounts.tsv>]   (표준 출력 또는 파일에 쓴다. 선언이 계약을 어기면 종료 코드 1)"""
import json
import sys

from boundary_contracts import validator


def rows(decl: dict) -> list:
    errs = [e.message[:200] for e in validator("DatabaseDeclaration").iter_errors(decl)]
    if errs:
        raise SystemExit("DatabaseDeclaration 위반: " + "; ".join(errs[:3]))
    names = [a["name"] for a in decl["accounts"]]
    if len(set(names)) != len(names):
        raise SystemExit("DatabaseDeclaration 위반: 계정 이름이 겹친다")
    for db in {a["database"] for a in decl["accounts"]}:
        if not any(a["database"] == db and a["privilege"] == "owner" for a in decl["accounts"]):
            raise SystemExit(f"DatabaseDeclaration 위반: 데이터베이스 {db}의 owner 계정이 없다(dml 계정은 소유자가 있어야 권한을 줄 수 있다)")
    return [(a["name"], a["database"], a["privilege"]) for a in decl["accounts"]]


if __name__ == "__main__":
    text = "".join("\t".join(r) + "\n" for r in rows(json.load(open(sys.argv[1], encoding="utf-8"))))
    if len(sys.argv) > 2:
        open(sys.argv[2], "w", encoding="utf-8").write(text)
    else:
        sys.stdout.write(text)
