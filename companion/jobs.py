"""Compile a settled job into the one authoritative web-exchange package."""
from __future__ import annotations

import hashlib
import json
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from companion.archives import safe_path
from companion.archives import MAX_MEMBER_BYTES, MAX_EXPANDED_BYTES, MAX_ARCHIVE_BYTES, MAX_MAIN_BYTES
from companion.core import _check_export_source, _copy_verified, _safe_name, _sha256, _read_json_object, prepare_call, request_paths
from companion.schema import errors, validator

TEXT = {"type": "string", "minLength": 1, "maxLength": 8000}
STRINGS = {"type": "array", "items": TEXT, "maxItems": 100}
JOB_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "required": ["title", "purpose", "task", "scope", "constraints", "requirements",
                 "deliverables", "sources", "stop_condition"],
    "properties": {
        "title": {**TEXT, "maxLength": 160}, "purpose": TEXT, "task": TEXT,
        "scope": {"type": "object", "additionalProperties": False,
                  "required": ["in", "out"], "properties": {"in": STRINGS, "out": STRINGS}},
        "constraints": STRINGS, "facts": STRINGS, "hypotheses": STRINGS, "assumptions": STRINGS,
        "unresolved_decisions": {"type": "array", "maxItems": 0},
        "stop_condition": TEXT, "result_schema": {"type": "object"},
        "requirements": {
            "type": "array", "minItems": 1, "maxItems": 50,
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["id", "requirement", "acceptance"],
                      "properties": {
                          "id": {"type": "string", "pattern": "^R[1-9][0-9]*$"},
                          "requirement": TEXT,
                          "acceptance": {
                              "type": "object", "additionalProperties": False,
                              "required": ["condition", "method", "evidence", "reviewer"],
                              "properties": {
                                  "condition": TEXT,
                                  "method": {"enum": ["review", "exists", "json-schema"]},
                                  "evidence": {"type": "array", "minItems": 1, "items": TEXT},
                                  "reviewer": {"enum": ["operator", "terminal", "mechanical"]},
                              },
                          },
                      }},
        },
        "deliverables": {
            "type": "array", "minItems": 1, "maxItems": 100,
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["filename", "description"],
                      "properties": {"filename": TEXT, "description": TEXT}},
        },
        "sources": {
            "type": "array", "maxItems": 1000,
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["path", "role"],
                      "properties": {"path": TEXT, "filename": TEXT, "role": TEXT}},
        },
    },
}


def check_job(job: dict) -> dict:
    if len(_bytes(job)) > MAX_MAIN_BYTES:
        raise ValueError("job metadata exceeds 2 MiB; move evidence into source files")
    problems = errors(JOB_SCHEMA, job)
    if problems:
        raise ValueError("job is not settled: " + "; ".join(problems))
    ids = [item["id"] for item in job["requirements"]]
    if len(ids) != len(set(ids)):
        raise ValueError("requirement IDs must be unique")
    names = [safe_path(item["filename"]) for item in job["deliverables"]]
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("deliverable paths must be unique after normalization")
    for requirement in job["requirements"]:
        acceptance = requirement["acceptance"]
        if acceptance["method"] == "review" and acceptance["reviewer"] == "mechanical":
            raise ValueError("semantic review cannot claim a mechanical reviewer")
        for evidence in acceptance["evidence"]:
            name = evidence.split("#", 1)[0]
            if name != "results" and name not in names:
                raise ValueError(f"unknown acceptance evidence: {evidence}")
        if acceptance["method"] == "json-schema" and acceptance["evidence"] != ["results"]:
            raise ValueError("json-schema acceptance currently supports results only")
    validator(job.get("result_schema", {"type": "object"}))
    return job


def job_brief(job: dict) -> str:
    check_job(job)
    short = lambda text: text[:240] + ("…" if len(text) > 240 else "")
    scope = short("; ".join(job["scope"]["in"]))
    outputs = ", ".join(item["filename"] for item in job["deliverables"])
    criteria = "; ".join(
        f'{item["id"]}: {short(item["acceptance"]["condition"])}' for item in job["requirements"][:5]
    )
    if len(job["requirements"]) > 5:
        criteria += f'; {len(job["requirements"]) - 5} more in JOB.json'
    return (f'{job["title"]}\nPurpose: {short(job["purpose"])}\nTask: {short(job["task"])}\n'
            f'Scope: {scope}\nOutside scope: {short("; ".join(job["scope"]["out"])) or "None specified"}\n'
            f'Constraints: {short("; ".join(job["constraints"])) or "None specified"}\n'
            f'Outputs: {short(outputs)}\nDone: {criteria}\nStop: {short(job["stop_condition"])}')


def _bytes(value) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def _source(root: Path, item: dict) -> Path:
    path = Path(item["path"]).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("source must be a regular file")
    _check_export_source(root, path)
    return path


def response_schema(job: dict, attempt: str, revision: str, archive: str) -> dict:
    ids = [item["id"] for item in job["requirements"]]
    artifacts = [safe_path(item["filename"]) for item in job["deliverables"]]
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object", "additionalProperties": False,
        "required": ["request_id", "job_revision", "status", "results", "criteria_report",
                     "artifacts_manifest", "limitations", "delivery"],
        "properties": {
            "request_id": {"const": attempt}, "job_revision": {"const": revision},
            "status": {"enum": ["COMPLETE", "PARTIAL", "BLOCKED"]},
            "results": {**job.get("result_schema", {"type": "object"}),
                        "$id": f"urn:externalcall:results:{revision}"},
            "criteria_report": {
                "type": "array", "minItems": len(ids), "maxItems": len(ids),
                "items": {"type": "object", "additionalProperties": False,
                          "required": ["id", "status", "evidence"],
                          "properties": {
                              "id": {"enum": ids},
                              "status": {"enum": ["MET", "UNMET", "UNKNOWN"]},
                              "evidence": {"type": "string", "maxLength": 8000}}},
                "allOf": [{"contains": {"properties": {"id": {"const": key}}}} for key in ids],
            },
            "artifacts_manifest": {
                "type": "array", "minItems": len(artifacts), "maxItems": len(artifacts),
                "items": {"type": "object", "additionalProperties": False,
                          "required": ["filename", "status", "media_type", "size", "sha256"],
                          "properties": {
                              "filename": {"enum": artifacts},
                              "status": {"enum": ["CREATED", "MISSING", "NOT_CREATED"]},
                              "media_type": TEXT, "size": {"type": "integer", "minimum": 0},
                              "sha256": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"}},
                          "allOf": [{
                              "if": {"properties": {"status": {"const": "CREATED"}}},
                              "then": {"properties": {"sha256": {"type": "string"}}},
                              "else": {"properties": {"size": {"const": 0}, "sha256": {"type": "null"}}},
                          }]},
                "allOf": [{"contains": {"properties": {"filename": {"const": name}}}}
                          for name in artifacts],
            },
            "limitations": STRINGS,
            "delivery": {"const": [archive]},
        },
    }


def prepare_job(root: Path, job: dict, now: datetime, *, cloned_from=None, repair_context=None) -> dict:
    check_job(job)
    root = root.resolve()
    token = uuid.uuid4().hex[:12]
    attempt = "ec_" + token
    main, archive = f"{attempt}_response.json", f"{attempt}_outputs.zip"
    with tempfile.TemporaryDirectory(prefix="externalcall-job-") as scratch:
        directory = Path(scratch)
        public = json.loads(json.dumps(job))
        public.pop("result_schema", None)
        public["sources"] = []
        inputs = []
        seen = {"job.json", "web_review_request.json", "web_response_schema.json",
                "package_response.py", "000_read_me_first.md"}
        for index, item in enumerate(job["sources"], 1):
            source = _source(root, item)
            filename = _safe_name(item.get("filename", f"source_{index:03}_{source.name}"), "source filename")
            if filename.casefold() in seen:
                raise ValueError(f"duplicate or reserved source filename: {filename}")
            seen.add(filename.casefold())
            snapshot = directory / filename
            _copy_verified(source, snapshot)
            digest, size = _sha256(snapshot)
            public["sources"].append({"filename": filename, "role": item["role"],
                                      "sha256": digest, "size": size})
            inputs.append({"path": str(snapshot), "filename": filename})
        # Include the task schema in the revision; changing it changes the job.
        public["result_schema"] = job.get("result_schema", {"type": "object"})
        revision = hashlib.sha256(_bytes(public)).hexdigest()
        request = {"request_id": attempt, "job_revision": revision, "job": public,
                   "expected_main_json": main, "expected_archive": archive,
                   "limits": {"member_bytes": MAX_MEMBER_BYTES, "expanded_bytes": MAX_EXPANDED_BYTES,
                              "archive_bytes": MAX_ARCHIVE_BYTES, "main_bytes": MAX_MAIN_BYTES}}
        generated = {
            "JOB.json": _bytes(public),
            "WEB_REVIEW_REQUEST.json": _bytes(request),
            "WEB_RESPONSE_SCHEMA.json": _bytes(response_schema(job, attempt, revision, archive)),
            "package_response.py": (Path(__file__).resolve().parents[1] / "scripts" / "package_response.py").read_bytes(),
        }
        if repair_context:
            generated["REPAIR_CONTEXT.json"] = _bytes(repair_context)
        for filename, contents in generated.items():
            path = directory / filename
            path.write_bytes(contents)
            inputs.append({"path": str(path), "filename": filename})
        prompt = (
            f"Return exactly one downloadable ZIP named {archive}.\n"
            f"It must contain {main} at its root and the agreed artifacts.\n"
            "Read WEB_REVIEW_REQUEST.json and WEB_RESPONSE_SCHEMA.json first.\n"
            "The frozen job defines purpose, scope, requirements, evidence and stopping conditions.\n"
            "Inspect sources according to their role; preserve stated constraints and hypotheses.\n"
            "Report gaps honestly with PARTIAL/BLOCKED and limitations.\n"
            "Do not claim a test ran unless it ran. Returned code will not be executed automatically.\n"
            "Use package_response.py if Python/file execution is available; it measures actual bytes.\n"
            "Artifact hashes cover artifact bytes only, never this JSON or the outer ZIP.\n"
            "No conversational text outside the downloadable ZIP.\n"
        )
        if repair_context:
            prompt += ("This is a mechanical correction of the same job. Read REPAIR_CONTEXT.json.\n"
                       "Preserve valid artifact bytes; correct the reported defects and regenerate metadata/ZIP.\n")
        manifest = prepare_call(root, {
            "subject": job["title"], "request_id": attempt, "expected_main_json": main,
            "expected_artifacts": [archive], "prompt_text": prompt, "input_files": inputs,
            "contract_version": 2, "job_revision": revision, "exchange_token": token,
            "expected_deliverables": [safe_path(x["filename"]) for x in job["deliverables"]],
            "job_brief": job_brief(job),
            "cloned_from": cloned_from,
            "repair_round": repair_context.get("round", 0) if repair_context else 0,
        }, now)
    manifest["job_brief"] = job_brief(job)
    return manifest


def reprepare_job(root: Path, exchange: Path, now: datetime, *, repair_context=None) -> dict:
    request_paths(root, exchange.name)
    request = _read_json_object(exchange / "request" / "WEB_REVIEW_REQUEST.json", "frozen request")
    job = request["job"]
    job["sources"] = [
        {"path": str(exchange / "request" / source["filename"]),
         "filename": source["filename"], "role": source["role"]}
        for source in job["sources"]
    ]
    # Frozen source bytes are explicitly re-exported by this internal operation.
    with tempfile.TemporaryDirectory(prefix="externalcall-resend-") as scratch:
        for source in job["sources"]:
            destination = Path(scratch) / source["filename"]
            _copy_verified(Path(source["path"]), destination)
            source["path"] = str(destination)
        return prepare_job(root, job, now, cloned_from=exchange.name, repair_context=repair_context)
