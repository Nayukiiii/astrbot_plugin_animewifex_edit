from __future__ import annotations

import base64
import logging
import os
import random
import re

import aiohttp

logger = logging.getLogger(__name__)


class GitHubPublisher:
    """Publish approved wife images to a GitHub-backed image repository."""

    def __init__(self, config: dict, list_cache_path: str, translation_profile_fn=None):
        self.config = config
        self.list_cache_path = list_cache_path
        self.translation_profile_fn = translation_profile_fn

    @property
    def token(self) -> str:
        return self.config.get("github_token", "")

    @property
    def repo(self) -> str:
        return self.config.get("github_repo", "")

    @property
    def branch(self) -> str:
        return self.config.get("github_branch", "main")

    def get_img_dir(self, source: str) -> str:
        """Pick the image directory used by an existing source, falling back to img2/img3."""
        if os.path.exists(self.list_cache_path):
            try:
                with open(self.list_cache_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        m = re.match(r"^(img\d+)/", line)
                        if not m:
                            continue
                        img_dir = m.group(1)
                        if img_dir == "img1":
                            continue
                        rest = line[len(img_dir) + 1:]
                        if "!" in rest and rest.split("!", 1)[0] == source:
                            return img_dir
            except Exception:
                pass
        return random.choice(["img2", "img3"])

    @staticmethod
    def detect_img_ext(data: bytes) -> str:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        if data[:3] == b"GIF":
            return ".gif"
        if data[:4] == b"\x00\x00\x01\x00":
            return ".ico"
        if data[:2] == b"BM":
            return ".bmp"
        if data[:3] == b"\xff\xd8\xff":
            return ".jpg"
        return ".jpg"

    @staticmethod
    def _safe_filename_part(text: str) -> str:
        return re.sub(r'[\\/:*?"<>|]', "_", text or "")

    @staticmethod
    def _safe_branch_part(text: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9]", "-", (text or "")[:20]).strip("-")
        return safe or "character"

    def _branch_char(self, source: str, char_name: str) -> str:
        if self.translation_profile_fn:
            try:
                trans = self.translation_profile_fn(char_name, source) or {}
                if trans.get("en_char"):
                    return trans["en_char"]
            except Exception:
                pass
        return char_name

    async def create_empty_pr(self, source: str, char_name: str, img_dir: str) -> str | None:
        """Create a PR with a placeholder and pre-updated list.txt for manual image upload."""
        if not self.token:
            return None

        safe_source = self._safe_filename_part(source)
        safe_char = self._safe_filename_part(char_name)
        filename = f"{img_dir}/{safe_source}!{safe_char}.jpg" if safe_source else f"{img_dir}/{safe_char}.jpg"

        safe_branch_char = self._safe_branch_part(self._branch_char(source, char_name))
        pr_branch = f"add-char-{safe_branch_char}-{random.randint(1000, 9999)}"
        placeholder_name = f".placeholder_{safe_branch_char}"

        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }
        base_url = f"https://api.github.com/repos/{self.repo}"
        logger.info("[github-publish] create empty PR branch=%s filename=%s", pr_branch, filename)

        try:
            async with aiohttp.ClientSession(headers=headers) as s:
                main_sha = await self._get_branch_sha(s, base_url)
                if not main_sha:
                    return None
                if not await self._create_branch(s, base_url, pr_branch, main_sha):
                    return None

                placeholder = base64.b64encode(f"请在此目录上传图片：{filename}".encode()).decode()
                ok = await self._put_file(
                    s, base_url, f"{img_dir}/{placeholder_name}",
                    message=f"Add: {source}!{char_name} (需手动上传图片)",
                    content_b64=placeholder,
                    branch=pr_branch,
                )
                if not ok:
                    return None

                await self._update_list_txt(s, base_url, pr_branch, [filename], source, char_name)

                body = (
                    f"新增角色：{source} - {char_name}\n\n"
                    f"自动拉图失败，请手动上传图片到分支 `{pr_branch}` 的以下路径：\n"
                    f"- `{filename}`\n\n"
                    f"上传图片后直接 merge 即可，list.txt 已预先更新。"
                )
                return await self._create_pull_request(
                    s, base_url, pr_branch,
                    title=f"Add: {source}!{char_name}",
                    body=body,
                )
        except Exception as e:
            logger.error("[github-publish] create empty PR failed: %s", e, exc_info=True)
            return None

    async def create_pr(
        self, source: str, char_name: str, img_dir: str, images: list[bytes]
    ) -> str | None:
        """Create a PR with fetched image files and list.txt updates."""
        if not self.token or not images:
            return None

        safe_source = self._safe_filename_part(source)
        safe_char = self._safe_filename_part(char_name)
        base = f"{safe_source}!{safe_char}" if safe_source else safe_char
        file_names = []
        for i, img_data in enumerate(images):
            suffix = "" if i == 0 else f"_{i + 1}"
            ext = self.detect_img_ext(img_data)
            file_names.append(f"{img_dir}/{base}{suffix}{ext}")

        safe_branch_char = self._safe_branch_part(self._branch_char(source, char_name))
        pr_branch = f"add-char-{safe_branch_char}-{random.randint(1000, 9999)}"
        headers = {
            "Authorization": f"token {self.token}",
            "Accept": "application/vnd.github.v3+json",
        }
        base_url = f"https://api.github.com/repos/{self.repo}"

        try:
            async with aiohttp.ClientSession(headers=headers) as s:
                main_sha = await self._get_branch_sha(s, base_url)
                if not main_sha:
                    return None
                if not await self._create_branch(s, base_url, pr_branch, main_sha):
                    return None

                for fname, img_data in zip(file_names, images):
                    ok = await self._put_file(
                        s, base_url, fname,
                        message=f"Add: {source}!{char_name}",
                        content_b64=base64.b64encode(img_data).decode(),
                        branch=pr_branch,
                    )
                    if not ok:
                        await s.delete(f"{base_url}/git/refs/heads/{pr_branch}")
                        return None

                await self._update_list_txt(s, base_url, pr_branch, file_names, source, char_name)

                body_lines = [
                    f"新增角色图片：{source} - {char_name}",
                    "",
                ] + [f"- `{f}`" for f in file_names] + [
                    "",
                    "merge 后 list.txt 已同步更新，无需额外操作。",
                ]
                return await self._create_pull_request(
                    s, base_url, pr_branch,
                    title=f"Add: {source}!{char_name}",
                    body="\n".join(body_lines),
                )
        except Exception as e:
            logger.error("[github-publish] create PR failed: %s", e)
            return None

    async def _get_branch_sha(self, session: aiohttp.ClientSession, base_url: str) -> str | None:
        async with session.get(f"{base_url}/git/ref/heads/{self.branch}") as r:
            if r.status != 200:
                logger.error("[github-publish] get branch SHA failed: %s %s", r.status, await r.text())
                return None
            return (await r.json())["object"]["sha"]

    async def _create_branch(self, session: aiohttp.ClientSession, base_url: str, branch: str, sha: str) -> bool:
        async with session.post(f"{base_url}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": sha}) as r:
            if r.status not in (200, 201):
                logger.error("[github-publish] create branch failed: %s %s", r.status, await r.text())
                return False
            return True

    async def _put_file(
        self, session: aiohttp.ClientSession, base_url: str, path: str,
        message: str, content_b64: str, branch: str,
    ) -> bool:
        async with session.put(
            f"{base_url}/contents/{path}",
            json={"message": message, "content": content_b64, "branch": branch},
        ) as r:
            if r.status not in (200, 201):
                logger.error("[github-publish] upload file failed: %s %s", r.status, await r.text())
                return False
            return True

    async def _update_list_txt(
        self, session: aiohttp.ClientSession, base_url: str, branch: str,
        entries: list[str], source: str, char_name: str,
    ) -> None:
        list_sha = None
        list_content_old = ""
        async with session.get(f"{base_url}/contents/list.txt", params={"ref": branch}) as r:
            if r.status == 200:
                data = await r.json()
                list_sha = data.get("sha")
                list_content_old = base64.b64decode(data["content"].replace("\n", "")).decode("utf-8")

        list_content_new = list_content_old.rstrip("\n") + "\n" + "\n".join(entries) + "\n"
        lines_sorted = sorted(set(l for l in list_content_new.splitlines() if l.strip()))
        list_content_new = "\n".join(lines_sorted) + "\n"
        list_encoded = base64.b64encode(list_content_new.encode("utf-8")).decode()

        put_body = {
            "message": f"Auto: update list.txt for {source}!{char_name}",
            "content": list_encoded,
            "branch": branch,
        }
        if list_sha:
            put_body["sha"] = list_sha
        async with session.put(f"{base_url}/contents/list.txt", json=put_body) as r:
            if r.status not in (200, 201):
                logger.error("[github-publish] update list.txt failed: %s %s", r.status, await r.text())

    async def _create_pull_request(
        self, session: aiohttp.ClientSession, base_url: str, branch: str, title: str, body: str
    ) -> str | None:
        async with session.post(
            f"{base_url}/pulls",
            json={"title": title, "head": branch, "base": self.branch, "body": body},
        ) as r:
            if r.status not in (200, 201):
                logger.error("[github-publish] create PR failed: %s %s", r.status, await r.text())
                return None
            return (await r.json()).get("html_url")
