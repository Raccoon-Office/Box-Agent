"""Small original-format archives for package/ACP regression tests."""
import hashlib
import io
import json
import zipfile


def archive_bytes(kind="expert", *, tool_requests=(), extra=(), bad_file_hash=False, wrapper=""):
    name = f"method-{kind}"
    manifest = {
        "schemaVersion": "1.0", "packageId": f"package-{kind}",
        "expertType": "agent" if kind == "expert" else "team", "entryAgent": "lead",
        "agents": [{"id": "lead", "file": "agents/lead.md", "skillBindings": [{"key": "method"}],
                    "toolRequests": list(tool_requests)}],
        "skills": [{"key": "method", "source": "BUNDLED", "path": "skills/skill-0"}],
    }
    files = {
        "agents/lead.md": f"Follow {kind} expert rules.".encode(),
        "skills/skill-0/SKILL.md": f"---\nname: {name}\ndescription: Method\n---\n{kind} method body".encode(),
        "skills/skill-0/references/style.txt": b"ORIGINAL_REFERENCE_OK\n",
    }
    if kind == "team":
        manifest["agents"].append({"id": "member", "file": "agents/member.md", "skillBindings": [{"key": "method"}]})
        manifest["team"] = {"leadAgent": "lead", "memberAgents": ["member"],
                            "workflow": "workflows/team.md", "executionMode": "orchestrated"}
        files.update({"agents/member.md": b"Review the work.", "workflows/team.md": b"Delegate and synthesize."})
    files[".box-agent-plugin/plugin.json"] = json.dumps(manifest).encode()
    records = [{"path": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
               for name, body in files.items()]
    if bad_file_hash:
        records[0]["sha256"] = "0" * 64
    files["files.json"] = json.dumps(records).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, body in files.items():
            info = zipfile.ZipInfo(wrapper + path, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, body)
        for path, body in extra:
            archive.writestr(path, body)
    return stream.getvalue()


def reference(body, ident="client-expert-id", **metadata):
    return {"id": ident, "name": "Client selection", **metadata,
            "packageDownload": {
                "downloadUrl": "https://desktop-skills.tos-s3-cn-shanghai.volces.com/package.zip?signature=PRIVATE_TICKET",
                "digest": hashlib.sha256(body).hexdigest(), "size": len(body),
            }}
