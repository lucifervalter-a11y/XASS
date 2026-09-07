from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from pc_client.e2e_crypto import SEALED_CONTENT_TYPE, generate_keypair, seal_bytes, unseal_bytes
from pc_client.remote_tools import _upload_bytes, delete_file, list_files, receive_uploaded_file


class RemoteToolsTests(unittest.TestCase):
    def test_only_allowed_roots_are_listed_and_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data = Path(directory) / "data"
            documents = home / "Documents"
            documents.mkdir(parents=True)
            target = documents / "notes.txt"
            target.write_text("hello", encoding="utf-8")
            with patch.dict(os.environ, {"USERPROFILE": str(home)}):
                listing = list_files(data, "documents", "")
                self.assertEqual(listing["entries"][0]["name"], "notes.txt")
                self.assertEqual(listing["entries"][0]["size"], 5)
                removed = delete_file(data, "documents", "notes.txt")
                self.assertEqual(removed["name"], "notes.txt")
                self.assertFalse(target.exists())

    def test_path_traversal_and_root_deletion_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            data = Path(directory) / "data"
            with patch.dict(os.environ, {"USERPROFILE": str(home)}):
                for path in ("../secret.txt", "folder/../../secret.txt", "C:/Windows/win.ini"):
                    with self.subTest(path=path), self.assertRaises(ValueError):
                        list_files(data, "documents", path)
                with self.assertRaises(ValueError):
                    delete_file(data, "documents", "")

    def test_agent_file_and_screenshot_uploads_keep_their_e2e_purpose(self) -> None:
        owner_private, owner_public = generate_keypair()
        agent_private, agent_public = generate_keypair()
        for kind in ("screenshot", "file_download"):
            with self.subTest(kind=kind):
                def respond(request):
                    self.assertEqual(request.headers["content-type"], SEALED_CONTENT_TYPE)
                    self.assertEqual(request.headers["x-xass-cipher"], "xass-sealed-v1")
                    self.assertEqual(request.url.params["kind"], kind)
                    self.assertNotIn(b"private bytes", request.content)
                    self.assertEqual(unseal_bytes(request.content, private_jwk=owner_private, peer_public_jwk=agent_public, aad=kind.encode()), b"private bytes")
                    return httpx.Response(200, json={"ok": True, "asset": {"token": "test"}})

                with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                    _upload_bytes(client, endpoint="https://agent.invalid", api_key="test", source_name="PC", command_id=1,
                                  kind=kind, filename="test.bin", content_type="application/octet-stream", body=b"private bytes",
                                  e2e_private_jwk=agent_private, owner_e2e_public_jwk=owner_public)

    def test_incoming_sealed_file_is_verified_before_writing_and_missing_keys_never_save_ciphertext(self) -> None:
        owner_private, owner_public = generate_keypair()
        agent_private, agent_public = generate_keypair()
        sealed = seal_bytes(b"private file", private_jwk=owner_private, peer_public_jwk=agent_public, aad=b"file_upload")
        wrong_private, _ = generate_keypair()
        cases = [
            ("missing", sealed, None, None),
            ("wrong-key", sealed, wrong_private, owner_public),
            ("corrupt-header", b"broken" + sealed[6:], agent_private, owner_public),
            ("valid", sealed, agent_private, owner_public),
        ]
        for label, body, private, peer in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                home = Path(directory) / "home"
                with patch.dict(os.environ, {"USERPROFILE": str(home)}), httpx.Client(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=body, headers={"Content-Type": SEALED_CONTENT_TYPE})
                )) as client:
                    arguments = dict(endpoint="https://agent.invalid", api_key="test", source_name="PC", root_name="documents",
                                     relative_path="", asset_token="test", filename="received.txt", e2e_private_jwk=private, owner_e2e_public_jwk=peer)
                    if label == "valid":
                        receive_uploaded_file(Path(directory) / "data", client, **arguments)
                        self.assertEqual((home / "Documents" / "received.txt").read_bytes(), b"private file")
                    else:
                        with self.assertRaises(ValueError):
                            receive_uploaded_file(Path(directory) / "data", client, **arguments)
                        self.assertEqual(list((home / "Documents").iterdir()), [])

    def test_plaintext_file_starting_with_brand_name_is_not_misdetected_as_ciphertext(self) -> None:
        agent_private, _ = generate_keypair()
        _, owner_public = generate_keypair()
        body = b"XASS ordinary exported text"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            with patch.dict(os.environ, {"USERPROFILE": str(home)}), httpx.Client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=body, headers={"Content-Type": "text/plain"})
            )) as client:
                receive_uploaded_file(Path(directory) / "data", client, endpoint="https://agent.invalid", api_key="test", source_name="PC",
                                      root_name="documents", relative_path="", asset_token="test", filename="plain.txt",
                                      e2e_private_jwk=agent_private, owner_e2e_public_jwk=owner_public)
            self.assertEqual((home / "Documents" / "plain.txt").read_bytes(), body)

    def test_unicode_upload_names_use_utf8_query_parameters_without_changing_ciphertext(self) -> None:
        owner_private, owner_public = generate_keypair()
        agent_private, agent_public = generate_keypair()
        cases = [("Домашний ПК", "договор.pdf"), ("Home PC", "Заметки.txt"), ("Ноутбук", "notes.txt")]
        for source_name, filename in cases:
            with self.subTest(source_name=source_name, filename=filename):
                def respond(request):
                    received_source = request.url.params.get("source_name") or request.headers.get("x-xass-source")
                    received_filename = request.url.params.get("filename") or request.headers.get("x-xass-filename")
                    self.assertEqual(received_source, source_name)
                    self.assertEqual(received_filename, filename)
                    self.assertTrue(request.headers["x-xass-filename"].isascii())
                    self.assertTrue(request.headers.get("x-xass-source", "").isascii())
                    self.assertEqual(request.headers["content-type"], SEALED_CONTENT_TYPE)
                    self.assertEqual(unseal_bytes(request.content, private_jwk=owner_private, peer_public_jwk=agent_public, aad=b"file_download"), b"private document")
                    return httpx.Response(200, json={"ok": True, "asset": {"token": "test", "filename": filename}})

                with httpx.Client(transport=httpx.MockTransport(respond)) as client:
                    result = _upload_bytes(client, endpoint="https://agent.invalid", api_key="test", source_name=source_name,
                                           command_id=1, kind="file_download", filename=filename, content_type="application/octet-stream",
                                           body=b"private document", e2e_private_jwk=agent_private, owner_e2e_public_jwk=owner_public)
                self.assertEqual(result["filename"], filename)

    def test_unicode_source_can_receive_and_decrypt_a_cyrillic_filename(self) -> None:
        owner_private, owner_public = generate_keypair()
        agent_private, agent_public = generate_keypair()
        blob = seal_bytes("Документ с iPhone".encode(), private_jwk=owner_private, peer_public_jwk=agent_public, aad=b"file_upload")
        def respond(request):
            self.assertEqual(request.url.params["source_name"], "Рабочий ПК")
            self.assertNotIn("x-xass-source", request.headers)
            return httpx.Response(200, content=blob, headers={"Content-Type": SEALED_CONTENT_TYPE})

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            with patch.dict(os.environ, {"USERPROFILE": str(home)}), httpx.Client(transport=httpx.MockTransport(respond)) as client:
                received = receive_uploaded_file(Path(directory) / "data", client, endpoint="https://agent.invalid", api_key="test",
                                                 source_name="Рабочий ПК", root_name="documents", relative_path="", asset_token="test",
                                                 filename="Документ.txt", e2e_private_jwk=agent_private, owner_e2e_public_jwk=owner_public)
            self.assertEqual(received["filename"], "Документ.txt")
            self.assertEqual((home / "Documents" / "Документ.txt").read_text(encoding="utf-8"), "Документ с iPhone")


if __name__ == "__main__":
    unittest.main()
