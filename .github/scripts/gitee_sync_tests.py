"""Regression checks for code and release mirroring without external credentials."""

from __future__ import annotations

import hashlib
import http.server
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

import sync_gitee as sync


class GitMirrorTests(unittest.TestCase):
    def test_all_refs_preserve_history_and_reject_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "work"
            source.mkdir()
            target = root / "gitee.git"
            sync.git("init", "-b", "main", cwd=source)
            sync.git("config", "user.name", "Mirror test", cwd=source)
            sync.git("config", "user.email", "mirror@example.invalid", cwd=source)
            sync.git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "initial", cwd=source)
            sync.git("branch", "feature/audio", cwd=source)
            sync.git("tag", "-a", "v1.0.0", "-m", "release", cwd=source)
            sync.git("init", "--bare", str(target), cwd=root)
            sync.sync_code(str(source), str(target), root / "first.git")
            initial = sync.git("rev-parse", "main", cwd=source)
            sync.git("update-ref", "refs/heads/gitee-only", initial, cwd=target)
            sync.git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "next", cwd=source)
            sync.sync_code(str(source), str(target), root / "second.git")
            self.assertEqual(sync.git("rev-parse", "main", cwd=target),
                             sync.git("rev-parse", "main", cwd=source))
            self.assertEqual(sync.git("rev-parse", "gitee-only", cwd=target), initial)
            self.assertEqual(sync.git("rev-parse", "v1.0.0", cwd=target),
                             sync.git("rev-parse", "v1.0.0", cwd=source))
            sync.git("checkout", "feature/audio", cwd=source)
            sync.git("-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "independent", cwd=source)
            other = sync.git("rev-parse", "HEAD", cwd=source)
            sync.git("fetch", str(source), "feature/audio", cwd=target)
            sync.git("update-ref", "refs/heads/main", other, cwd=target)
            with self.assertRaises(subprocess.CalledProcessError):
                sync.sync_code(str(source), str(target), root / "conflict.git")
            self.assertEqual(sync.git("rev-parse", "main", cwd=target), other)

    def test_repository_without_tags_syncs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "work"
            source.mkdir()
            sync.git("init", "-b", "main", cwd=source)
            sync.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "initial", cwd=source)
            target = root / "target.git"
            sync.git("init", "--bare", str(target), cwd=root)
            sync.sync_code(str(source), str(target), root / "copy.git")
            self.assertEqual(sync.git("rev-parse", "main", cwd=target),
                             sync.git("rev-parse", "main", cwd=source))


class FakeGitee:
    def __init__(self) -> None:
        self.release = None
        self.files = {}
        self.uploads = []
        self.creates = 0
        self.fail_upload = ""

    def request(self, path: str, method: str = "GET", data: dict | None = None,
                file: Path | None = None) -> object:
        if method == "GET":
            return self.release
        if file is not None:
            if file.name == self.fail_upload:
                raise sync.SyncError("Gitee POST attachment: HTTP 403")
            self.files[file.name] = file.read_bytes()
            self.uploads.append(file.name)
            return {"id": len(self.files)}
        if method == "POST":
            self.creates += 1
            self.release = {"id": 12, **data}
        else:
            self.release.update(data)
        return self.release

    def attachments(self, release_id: int) -> list[dict]:
        return [{"name": name, "browser_download_url": "https://gitee.test/" + name}
                for name in self.files]


class ReleaseMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.api = FakeGitee()
        self.packages = {
            "pr2resolve-v1.0.0-windows-x86_64.zip": b"windows package",
            "pr2resolve-v1.0.0-macos-arm64.tar.gz": b"macos package",
            "pr2resolve-v1.0.0-linux-x86_64.tar.gz": b"linux package",
        }
        self.manifest = "".join(f"{hashlib.sha256(body).hexdigest()}  {name}\n"
                                for name, body in self.packages.items()).encode()
        self.github_files = {**self.packages, "SHA256SUMS.txt": self.manifest}
        self.source = {
            "tag_name": "v1.0.0", "name": "A release", "body": "说明 / notes",
            "draft": False, "prerelease": False,
            "assets": [{"name": name, "size": len(body),
                        "browser_download_url": "https://github.test/" + name}
                       for name, body in self.github_files.items()],
        }
        download = patch.object(sync, "download", side_effect=self.download)
        download.start()
        self.addCleanup(download.stop)

    def download(self, url: str, destination: Path) -> None:
        files = self.github_files if url.startswith("https://github.test/") else self.api.files
        destination.write_bytes(files[url.rsplit("/", 1)[1]])

    def run_sync(self) -> None:
        sync.sync_release(self.api, self.source, self.folder, "a" * 40)

    def test_release_upload_and_repeat_preserve_identical_bytes(self) -> None:
        self.run_sync()
        self.run_sync()
        self.assertEqual(self.api.files, self.github_files)
        self.assertEqual(self.api.creates, 1)
        self.assertEqual(len(self.api.uploads), 4)
        self.assertEqual(self.api.release["body"], self.source["body"])
        self.assertEqual(self.api.release["target_commitish"], "a" * 40)

    def test_partial_upload_resumes_without_duplicate_assets(self) -> None:
        failed_name = list(self.packages)[1]
        self.api.fail_upload = failed_name
        with self.assertRaisesRegex(sync.SyncError, "403"):
            self.run_sync()
        self.api.fail_upload = ""
        self.run_sync()
        self.assertEqual(self.api.creates, 1)
        self.assertEqual(len(self.api.uploads), 4)
        self.assertEqual(self.api.files, self.github_files)

    def test_existing_conflicting_asset_is_not_overwritten(self) -> None:
        self.run_sync()
        name = next(iter(self.packages))
        self.api.files[name] = b"independent modification"
        with self.assertRaisesRegex(sync.SyncError, "conflict"):
            self.run_sync()
        self.assertEqual(self.api.files[name], b"independent modification")
        self.assertEqual(len(self.api.uploads), 4)

    def test_corrupt_github_package_does_not_create_release(self) -> None:
        name = next(iter(self.packages))
        self.github_files[name] = b"x" * len(self.github_files[name])
        with self.assertRaisesRegex(sync.SyncError, "checksum mismatch"):
            self.run_sync()
        self.assertEqual(self.api.creates, 0)

    def test_legacy_build_prefixes_preserve_original_manifest(self) -> None:
        manifest = "".join(f"{hashlib.sha256(body).hexdigest()}  build-platform/{name}\n"
                           for name, body in self.packages.items()).encode()
        self.github_files["SHA256SUMS.txt"] = manifest
        self.source["assets"][-1]["size"] = len(manifest)
        self.run_sync()
        self.assertEqual(self.api.files["SHA256SUMS.txt"], manifest)
        self.assertEqual(self.api.files, self.github_files)

    def test_bad_checksum_manifest_is_rejected(self) -> None:
        for manifest in [b"not a checksum\n", self.manifest + self.manifest,
                         self.manifest.replace(b"pr2resolve-", b"../pr2resolve-", 1)]:
            with self.subTest(manifest=manifest):
                (self.folder / "SHA256SUMS.txt").write_bytes(manifest)
                with self.assertRaises(sync.SyncError):
                    sync.verify_packages(self.folder, "v1.0.0")

    def test_drafts_and_prereleases_are_rejected(self) -> None:
        for key in ["draft", "prerelease"]:
            with self.subTest(key=key):
                source = {**self.source, key: True}
                with self.assertRaisesRegex(sync.SyncError, "published stable"):
                    sync.sync_release(self.api, source, self.folder, "a" * 40)
        self.assertEqual(self.api.creates, 0)

    def test_uploaded_bytes_are_checked_by_downloading_again(self) -> None:
        upload = self.api.request

        def corrupt_upload(path: str, method: str = "GET", data: dict | None = None,
                           file: Path | None = None) -> object:
            result = upload(path, method, data, file)
            if file is not None:
                self.api.files[file.name] = b"corrupted on server"
            return result

        with patch.object(self.api, "request", side_effect=corrupt_upload):
            with self.assertRaisesRegex(sync.SyncError, "upload checksum mismatch"):
                self.run_sync()

    def test_release_notes_update_reuses_existing_attachments(self) -> None:
        self.run_sync()
        self.source["body"] = "Updated notes"
        self.run_sync()
        self.assertEqual(self.api.release["body"], "Updated notes")
        self.assertEqual(len(self.api.uploads), 4)


class ApiTests(unittest.TestCase):
    def test_multipart_upload_sends_file_and_authentication(self) -> None:
        received = {}

        class UploadHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                received['authorization'] = self.headers.get('Authorization')
                received['body'] = self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(201)
                self.end_headers()
                self.wfile.write(b'{"id": 7}')

            def log_message(self, format: str, *args: object) -> None:
                pass

        server = http.server.HTTPServer(('127.0.0.1', 0), UploadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                file = Path(temp) / 'package.zip'
                file.write_bytes(b'actual package contents')
                api = sync.Gitee('owner', 'repo', 'test-token')
                api.base = f'http://127.0.0.1:{server.server_port}'
                self.assertEqual(api.request('/attachments', 'POST', file=file), {'id': 7})
                self.assertEqual(received['authorization'], 'Bearer test-token')
                self.assertIn(file.read_bytes(), received['body'])
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_upload_transport_errors_do_not_expose_credentials(self) -> None:
        api = sync.Gitee('owner', 'repo', 'secret-value')
        with patch.object(sync.subprocess, 'run', return_value=Mock(
            returncode=28, stderr='Operation timed out: secret-value'
        )):
            with self.assertRaisesRegex(sync.SyncError, 'curl 28') as error:
                api.request('/attachments', 'POST', file=Path('package.zip'))
            self.assertNotIn('secret-value', str(error.exception))

    def test_api_permission_and_upload_errors_are_not_hidden_or_leaked(self) -> None:
        api = sync.Gitee("owner", "repo", "secret-value")
        for method, code in [("GET", 401), ("GET", 403), ("POST", 413), ("POST", 500)]:
            with self.subTest(method=method, code=code):
                failure = urllib.error.HTTPError("https://gitee.com/", code, "secret-value", {}, None)
                with patch("urllib.request.build_opener") as opener:
                    opener.return_value.open.side_effect = failure
                    with self.assertRaisesRegex(sync.SyncError, f"HTTP {code}") as error:
                        api.request("/releases", method, data={} if method == "POST" else None)
                    self.assertNotIn("secret-value", str(error.exception))

    def test_only_missing_release_is_treated_as_absent(self) -> None:
        api = sync.Gitee("owner", "repo", "token")
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.HTTPError("url", 404, "missing", {}, None)
            self.assertIsNone(api.request("/releases/tags/v1.0.0"))
            with self.assertRaisesRegex(sync.SyncError, "404"):
                api.request("")

    def test_network_failure_reports_reason_without_exposing_token(self) -> None:
        api = sync.Gitee("owner", "repo", "secret-value")
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = urllib.error.URLError(
                TimeoutError("upload timed out: secret-value"))
            with self.assertRaisesRegex(sync.SyncError, "TimeoutError: upload timed out") as error:
                api.request("/releases/42/attach_files", "POST", data={})
            self.assertNotIn("secret-value", str(error.exception))

    def test_attachment_listing_reads_all_pages(self) -> None:
        api = sync.Gitee("owner", "repo", "token")
        with patch.object(api, "request", side_effect=[[{"id": n} for n in range(100)], [{"id": 100}]]) as request:
            self.assertEqual(len(api.attachments(42)), 101)
            self.assertIn("page=2", request.call_args.args[0])

    def test_redirect_drops_credentials_and_rejects_http(self) -> None:
        request = urllib.request.Request("https://gitee.com/api/v5/test",
                                         headers={"Authorization": "Bearer secret"})
        redirect = sync.DownloadRedirect()
        result = redirect.redirect_request(request, None, 302, "Found", {}, "https://storage.test/file")
        self.assertIsNone(result.get_header("Authorization"))
        with self.assertRaisesRegex(sync.SyncError, "HTTPS"):
            redirect.redirect_request(request, None, 302, "Found", {}, "http://storage.test/file")


if __name__ == "__main__":
    unittest.main()
