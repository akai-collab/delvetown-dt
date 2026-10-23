#!/usr/bin/env python3
"""Open a pull request on Tangled from a shell: upload the patch, write the sh.tangled.repo.pull record.

Tangled has no `gh` and no documented pull endpoint: a pull is an ordinary ATProto record in the opener's own
repository, so it can be filed with two XRPC calls. Written for an agent with a terminal, a repo and no browser,
and used for the two pulls on kaiasky.delve.town/delvetown-quietyear on 2026-10-23.

    git push origin mybranch
    git format-patch master..mybranch --stdout | gzip -9 > /tmp/pr.patch.gz
    tangled-pull.py --repo-did did:plc:... --source mybranch --title "..." --body-file /tmp/body.md \
                    --patch-gz /tmp/pr.patch.gz

--repo-did is the REPOSITORY's DID, not the account's: read `value.repoDid` from the target repo's own
`sh.tangled.repo` record. A fork carries its own DID, which is why a fork-opened pull names a different
`source.repo`.

The token comes from this client's own session (dt.session), in-process, and is never printed.
"""
import argparse
import datetime
import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import dt  # noqa: E402  (the sibling client: PDS, session, user_agent)


def upload(token: str, path: str) -> dict:
    data = pathlib.Path(path).read_bytes()
    req = urllib.request.Request(
        dt.PDS + "/xrpc/com.atproto.repo.uploadBlob", data=data,
        headers={"Content-Type": "application/gzip", "Accept": "application/json",
                 "User-Agent": dt.user_agent(), "Authorization": "Bearer " + token}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())["blob"]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--repo-did", required=True, help="the repository's DID (sh.tangled.repo value.repoDid)")
    p.add_argument("--source", required=True, help="branch the change is on")
    p.add_argument("--target", default="master")
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--patch-gz", required=True, help="git format-patch base..branch --stdout | gzip -9")
    args = p.parse_args()

    s = dt.session()
    tok, did = s["accessJwt"], s["did"]

    try:
        blob = upload(tok, args.patch_gz)
    except urllib.error.HTTPError as e:
        print("uploadBlob failed:", e.code, e.read().decode()[:400])
        return 1
    print("blob:", blob["ref"]["$link"], blob["size"], "bytes")

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    rec = {
        "$type": "sh.tangled.repo.pull",
        "title": args.title,
        "body": pathlib.Path(args.body_file).read_text(encoding="utf-8"),
        "rounds": [{"createdAt": stamp, "patchBlob": blob}],
        "source": {"repo": args.repo_did, "branch": args.source, "repoDid": args.repo_did},
        "target": {"repo": args.repo_did, "branch": args.target, "repoDid": args.repo_did},
        "createdAt": stamp,
    }
    payload = {"repo": did, "collection": "sh.tangled.repo.pull", "record": rec}
    req = urllib.request.Request(
        dt.PDS + "/xrpc/com.atproto.repo.createRecord", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": dt.user_agent(), "Authorization": "Bearer " + tok}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print("createRecord failed:", e.code, e.read().decode()[:600])
        return 1
    print("pull:", out.get("uri"), out.get("cid"))
    print("now read the pulls page and check the diff has lines: the record being accepted is not placement")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
