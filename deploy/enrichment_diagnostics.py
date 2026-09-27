"""One-shot read-only catalog diagnosis; stdout is recipient-encrypted JSON only.

Executed via pinned SSH stdin, never installed. No media, settings, logs, DB
writes or credentials are exported. The only plaintext is bounded process RAM.
"""
from __future__ import annotations

import asyncio
import base64
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time
import unicodedata
from types import SimpleNamespace
from urllib.parse import urlsplit

MAGIC = b"XASS-ENRICHMENT-DIAGNOSTIC-1"
MAX_REPORT_BYTES = 256 * 1024
TRACK_LIMIT = 10
LIVE_LIMIT = 3


def request_value(encoded):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    if not isinstance(encoded, str) or len(encoded) > 8192:
        raise ValueError("Invalid request")
    value = json.loads(base64.b64decode(encoded, validate=True))
    if not re.fullmatch(r"[a-z0-9-]{1,80}", value.get("id", "")):
        raise ValueError("Invalid request")
    if type(value.get("expires")) is not int or not time.time() < value["expires"] <= time.time() + 86400:
        raise ValueError("Expired request")
    recipient = serialization.load_pem_public_key(base64.b64decode(value["recipient"], validate=True))
    if not isinstance(recipient, rsa.RSAPublicKey) or not 3072 <= recipient.key_size <= 4096:
        raise ValueError("Invalid recipient")
    return value, recipient


def encrypt(report, request, recipient):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("Report too large")
    key, nonce = os.urandom(32), os.urandom(12)
    aad = MAGIC + b":" + request["id"].encode("ascii")
    wrapped = recipient.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=MAGIC))
    b64 = lambda value: base64.b64encode(value).decode("ascii")
    return {"format": MAGIC.decode(), "id": request["id"], "wrapped_key": b64(wrapped),
            "nonce": b64(nonce), "ciphertext": b64(AESGCM(key).encrypt(nonce, raw, aad))}


def parsed(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def compact(value):
    value = parsed(value)
    lyrics = parsed(value.get("lyrics"))
    return {key: value.get(key) for key in ("status", "reason", "lookup_status", "lookup_reason", "using_cached_result", "retry_after")
            if key in value} | {
        "candidate": value.get("candidate"), "candidates": (value.get("candidates") or [])[:3],
        "artwork": value.get("artwork"),
        "lyrics": {"source": lyrics.get("source"), "status": lyrics.get("status"), "synced": lyrics.get("synced"),
                   "text_characters": len(lyrics.get("text") or ""), "lines": len(lyrics.get("lines") or [])}}


def failure_category(error):
    chain = []
    while error is not None and len(chain) < 5:
        chain.append(error)
        error = error.__cause__
    raw = " ".join(str(item).lower() for item in chain)
    if "eof" in raw:
        return "tls_eof"
    if "certificate" in raw:
        return "certificate_error"
    if "name resolution" in raw or "getaddrinfo" in raw:
        return "dns_error"
    if any("timeout" in type(item).__name__.lower() for item in chain):
        return "timeout"
    return "provider_unavailable"


def tls_environment():
    """Public MusicBrainz endpoint only; both trust modes verify certificates."""
    import certifi
    defaults = ssl.get_default_verify_paths()
    result = {"server_clock_utc": datetime.now(timezone.utc).isoformat(), "openssl": ssl.OPENSSL_VERSION,
              "certifi_version": getattr(certifi, "__version__", "unknown"),
              "certifi_bundle_present": Path(certifi.where()).is_file(),
              "default_cafile_present": bool(defaults.cafile and Path(defaults.cafile).is_file()),
              "default_capath_present": bool(defaults.capath and Path(defaults.capath).is_dir()),
              "ssl_cert_file_env_present": bool(os.environ.get("SSL_CERT_FILE")),
              "ssl_cert_dir_env_present": bool(os.environ.get("SSL_CERT_DIR")), "tls": [], "curl": []}
    try:
        result["musicbrainz_addresses"] = sorted({entry[4][0] for entry in socket.getaddrinfo("musicbrainz.org", 443, type=socket.SOCK_STREAM)})
    except OSError:
        result["dns_error"] = True
    for source in ("system", "certifi"):
        started = time.monotonic()
        item = {"trust_source": source}
        try:
            context = ssl.create_default_context(cafile=certifi.where() if source == "certifi" else None)
            with socket.create_connection(("musicbrainz.org", 443), timeout=4) as raw:
                item["peer_address"] = raw.getpeername()[0]
                with context.wrap_socket(raw, server_hostname="musicbrainz.org") as verified:
                    cert = verified.getpeercert()
                    item.update(verified=True, protocol=verified.version(), expires=cert.get("notAfter"))
        except ssl.SSLCertVerificationError as error:
            item.update(verified=False, verify_code=error.verify_code, verify_message=error.verify_message[:160])
        except (OSError, TimeoutError) as error:
            item.update(verified=False, error_category=failure_category(error))
        item["elapsed_ms"] = round(1000 * (time.monotonic() - started))
        result["tls"].append(item)
        try:
            command = ["curl", "--noproxy", "*", "--connect-timeout", "4", "--max-time", "7", "--silent", "--show-error",
                       "--output", os.devnull, "--write-out", '{"http_status":"%{http_code}","ssl_verify_result":%{ssl_verify_result}}']
            if source == "certifi":
                command.extend(["--cacert", certifi.where()])
            command.append("https://musicbrainz.org/")
            output = subprocess.run(command, capture_output=True, text=True, timeout=9)
            status = json.loads(output.stdout) if output.stdout else {}
            result["curl"].append({"trust_source": source, "returncode": output.returncode, **status})
        except Exception:
            result["curl"].append({"trust_source": source, "error_category": "curl_probe_unavailable"})
    return result


async def read_tracks(database_url):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    # Already deployed, independently tested read-only URL policy; no app.main.
    from deploy.music_diagnostics import readonly_url
    url = readonly_url(database_url)
    postgres = url.get_backend_name() == "postgresql"
    engine = create_async_engine(url, echo=False, connect_args={"timeout": 5, **({"command_timeout": 5} if postgres else {})})
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SET TRANSACTION READ ONLY" if postgres else "PRAGMA query_only = ON"))
            if postgres:
                await connection.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            result = await connection.execute(text(f"""SELECT t.id, t.title, t.artist, t.album, t.filename,
                t.duration, t.created_at, e.checked_at, e.dismissed, e.revision,
                substr(CAST(e.original AS TEXT),1,4096) AS original,
                substr(CAST(e.result AS TEXT),1,196608) AS result,
                CASE WHEN e.artwork_data IS NULL THEN 0 ELSE 1 END AS has_artwork,
                CASE WHEN e.owner_lyrics IS NULL OR CAST(e.owner_lyrics AS TEXT) = '{{}}' THEN 0 ELSE 1 END AS has_owner_lyrics
                FROM music_tracks t LEFT JOIN music_enrichment e ON e.track_id=t.id
                WHERE t.deleted = FALSE ORDER BY t.created_at DESC, t.id DESC LIMIT {TRACK_LIMIT}"""))
            return [dict(row) for row in result.mappings()]
    finally:
        await engine.dispose()


async def collect(database_url):
    import httpx
    from app.services import music_enrichment as catalog
    rows = await read_tracks(database_url)

    class ObservedCatalog(catalog.MusicEnrichmentService):
        def __init__(self):
            self.network = []
            owner = self
            class Transport(httpx.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    # Each request owns its transport so the service's short-lived
                    # clients cannot reuse a closed connection pool.
                    self.inner = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
                    started = time.monotonic()
                    item = {"provider": request.url.host}
                    owner.network.append(item)
                    try:
                        response = await self.inner.handle_async_request(request)
                        item["http_status"] = response.status_code
                        item["content_encoding"] = response.headers.get("content-encoding", "identity")[:40]
                        return response
                    except Exception as error:
                        item["error_category"] = failure_category(error)
                        raise
                    finally:
                        item["headers_elapsed_ms"] = round(1000 * (time.monotonic() - started))
                async def aclose(self):
                    if hasattr(self, "inner"):
                        await self.inner.aclose()
            super().__init__(transport=Transport())
            self.observations = []
            self.signature = {}

        async def _json(self, url, params):
            host = urlsplit(url).hostname
            observation = {"provider": host if host in {"lrclib.net", "musicbrainz.org"} else "other",
                           "query": params, "route": "selected_id" if "/api/get/" in url else "search"}
            self.observations.append(observation)
            network_start = len(self.network)
            started = time.monotonic()
            try:
                value = await super()._json(url, params)
                observation["response"] = "not_found" if value is None else "json_received"
                candidates = (catalog._lrclib_rows(value if isinstance(value, list) else [value]) if host == "lrclib.net"
                              else catalog._musicbrainz_rows(value or {"recordings": []}, self.signature.get("album", ""))) if value is not None else []
                observation["records"] = len(candidates)
                observation["matching_reasons"] = dict(Counter(catalog._match(self.signature, candidate) for candidate, _ in candidates))
                observation["candidate_sample"] = [candidate for candidate, _ in candidates[:3]]
                return value
            except catalog._ProviderFailure as error:
                observation["response"] = error.status
                observation["error_category"] = failure_category(error)
                observation["retry_after"] = error.retry_after
                raise
            except (ValueError, TypeError):
                observation["response"] = "invalid_provider_payload"
                raise
            finally:
                observation["elapsed_ms"] = round(1000 * (time.monotonic() - started))
                observation["http"] = self.network[network_start:]

    service = ObservedCatalog()
    report = {"schema": 2, "created_at": datetime.now(timezone.utc).isoformat(),
              "limits": {"tracks": TRACK_LIMIT, "live": LIVE_LIMIT}, "tracks": []}
    try:
        revision = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=3, check=True).stdout.strip()
        report["server_revision"] = revision if re.fullmatch(r"[0-9a-f]{40}", revision) else "unknown"
    except Exception:
        report["server_revision"] = "unknown"
    for index, row in enumerate(rows):
        saved = parsed(row.pop("result"))
        original = parsed(row.pop("original"))
        track = SimpleNamespace(**{field: row[field] for field in ("id", "title", "artist", "album", "filename", "duration")},
                                is_excerpt=bool(original.get("is_excerpt")))
        track.is_excerpt = track.is_excerpt or bool(catalog._CUT.search(
            (str(original.get("title", row["title"])) + " " + row["filename"]).replace("_", " ")))
        row = {key: value.isoformat() if isinstance(value, datetime) else value for key, value in row.items()}
        row["stored"] = compact(saved)
        row["normalized_signature"] = catalog._signature(track)
        if index < LIVE_LIMIT:
            service.observations = []
            selected = saved.get("candidate") if saved.get("status") == "confirmed" and not row["dismissed"] else None
            signature_track = SimpleNamespace(**vars(track))
            if selected:
                for key in ("title", "artist", "album"):
                    setattr(signature_track, key, selected.get(key, ""))
            service.signature = catalog._signature(signature_track)
            try:
                async with asyncio.timeout(20):
                    result = await service.confirm(track, selected) if selected else await service.enrich(track)
                row["live"] = compact(result)
            except TimeoutError:
                row["live"] = {"status": "diagnostic_timeout"}
            except Exception:
                row["live"] = {"status": "diagnostic_failed"}
            row["provider_observations"] = service.observations
            signature = catalog._signature(track)
            raw_title, raw_artist = signature["title"], signature["artist"]
            # Comparison only: do not change the DB, stored tags, or catalog code.
            raw_title_for_cleanup = track.title
            if not track.artist and len(parts := re.split(r"\s+[-–—]\s+", raw_title_for_cleanup)) == 2:
                raw_title_for_cleanup = parts[1]
            nfc_title = unicodedata.normalize("NFC", raw_title)
            nfc_artist = unicodedata.normalize("NFC", raw_artist)
            variants = [("raw", raw_title, raw_artist), ("NFC", nfc_title, nfc_artist)]
            if re.search(r"\s*\[facetext\]\s*$", raw_title_for_cleanup, flags=re.I):
                clean = re.sub(r"\s*\[facetext\]\s*$", "", raw_title_for_cleanup, flags=re.I)
                variants.append(("NFC_without_exact_facetext_suffix", unicodedata.normalize("NFC", clean), nfc_artist))
            row["lrclib_query_comparison"] = []
            for mode, title, artist in variants:
                service.observations = []
                service.signature = {**signature, "title": title, "artist": artist, "query": f"{artist} {title}".strip()}
                comparison = {"mode": mode}
                try:
                    async with asyncio.timeout(13):
                        await service._json("https://lrclib.net/api/search", {"track_name": title, "artist_name": artist})
                except (catalog._ProviderFailure, TimeoutError, ValueError, TypeError) as error:
                    comparison["error_category"] = failure_category(error)
                comparison["observations"] = service.observations
                row["lrclib_query_comparison"].append(comparison)
        report["tracks"].append(row)
    report["public_tls_environment"] = await asyncio.to_thread(tls_environment)
    return report


def main():
    logging.disable(logging.CRITICAL)
    try:
        request, recipient = request_value(sys.argv[1])
        sys.path.insert(0, str(Path.cwd()))
        # Capture any unexpected library print, never leak it to public CI logs.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            from app.config import Settings
            async def bounded():
                return await asyncio.wait_for(collect(Settings().database_url), timeout=170)
            report = asyncio.run(bounded())
            envelope = encrypt(report, request, recipient)
        print(json.dumps(envelope, separators=(",", ":")), flush=True)
        return 0
    except BaseException:
        # No exception text, DSN, title, query, filename, raw provider body or key.
        print('{"ok":false,"error_category":"diagnostics_unavailable"}', file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
