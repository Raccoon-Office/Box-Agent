"""Public references to versioned outputs of a delegated device turn."""
from typing import Any


def artifact_refs(result: Any, device: str) -> list[dict[str, str]]:
    if not isinstance(result, dict) or "artifactRefs" not in result:
        return []
    items = result["artifactRefs"]
    if not isinstance(items, list) or len(items) > 64:
        raise ValueError("invalid device artifact manifest")
    refs = []
    seen = set()
    for item in items:
        if not isinstance(item, dict) or item.get("deviceId") != device:
            raise ValueError("device artifact identity mismatch")
        if not all(isinstance(item.get(k), str) and 0 < len(item[k].strip()) <= 512
                   for k in ("artifactId", "name")):
            raise ValueError("invalid device artifact identity")
        if item["artifactId"] in seen or any(c in item["name"] for c in ("/", "\\", "\x00")):
            raise ValueError("invalid device artifact name or duplicate ID")
        seen.add(item["artifactId"])
        ref = {k: item[k] for k in ("deviceId", "artifactId", "name")}
        if "cloudFileRef" in item:
            uri = item["cloudFileRef"]
            if not isinstance(uri, str) or not uri.startswith("file:") or len(uri) <= 5 or len(uri) > 32768:
                raise ValueError("invalid cloud file reference")
            ref["cloudFileRef"] = uri
        refs.append(ref)
    return refs


def delivered_update(record: dict, current: dict) -> dict | None:
    """A terminal turn can acquire cloud locations for already published outputs."""
    if (record["status"] != current["status"] or (record["status"] != "succeeded"
            and record.get("deliveryStatus") != "failed")):
        return None
    previous = artifact_refs(record.get("result"), record["deviceId"])
    incoming = artifact_refs(current.get("result"), record["deviceId"])
    indexed = {item["artifactId"]: item for item in previous}
    changed = False
    for item in incoming:
        old = indexed.get(item["artifactId"])
        if old is None or old["name"] != item["name"]:
            raise ValueError("unknown terminal device artifact")
        if uri := item.get("cloudFileRef"):
            if old.get("cloudFileRef", uri) != uri:
                raise ValueError("conflicting cloud file reference")
            if "cloudFileRef" not in old:
                old["cloudFileRef"] = uri
                changed = True
    if changed:
        return {**record, "seq": current["seq"],
                "readySeq": record.get("readySeq", record["seq"]),
                "result": {**record["result"], "artifactRefs": previous}}
    return None


def project_artifacts(value: dict, required: bool) -> dict:
    result = value.get("result")
    refs = artifact_refs(result, value.get("deviceId", ""))
    if isinstance(result, dict) and "artifactRefs" in result:
        value = {**value, "result": {**result, "artifactRefs": refs}}
    if required and value.get("status") == "succeeded":
        if not refs:
            return {**value, "status": "failed", "deliveryStatus": "failed",
                    "complete": True, "error": "NO_DELIVERABLE_ARTIFACTS"}
        if any("cloudFileRef" not in r for r in refs):
            return {**value, "status": "running", "deliveryStatus": "pending", "complete": False}
    return value
