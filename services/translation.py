from __future__ import annotations

import json
import os


class TranslationCache:
    """Persistent character translation profile cache.

    The historical cache only stored {"en": "...", "alt": [...]}.  Newer code
    stores a full profile while keeping those legacy keys for compatibility.
    """

    def __init__(self, path: str):
        self.path = path

    def load(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

    @staticmethod
    def key(char: str, source: str = "") -> str:
        char = (char or "").strip()
        source = (source or "").strip()
        return f"{char}|{source}" if source else char

    @staticmethod
    def normalize(entry: dict | str | None, char: str = "", source: str = "") -> dict:
        if not isinstance(entry, dict):
            entry = {"en": str(entry or "")}
        alt = entry.get("alt_char", entry.get("alt", []))
        if not isinstance(alt, list):
            alt = []
        data = {
            "zh_char": str(entry.get("zh_char", "") or "").strip(),
            "en_char": str(entry.get("en_char", entry.get("en", "")) or "").strip(),
            "ja_char": str(entry.get("ja_char", "") or "").strip(),
            "kana_char": str(entry.get("kana_char", "") or "").strip(),
            "alt_char": list(dict.fromkeys(str(x).strip() for x in alt if str(x).strip())),
            "en_source": str(entry.get("en_source", "") or "").strip(),
            "ja_source": str(entry.get("ja_source", "") or "").strip(),
            "short_source": str(entry.get("short_source", "") or "").strip(),
            "is_vtuber": bool(entry.get("is_vtuber", False)),
        }
        if char and not data["zh_char"] and any("\u4e00" <= c <= "\u9fff" for c in char):
            data["zh_char"] = char
        if source and not data["ja_source"] and any("\u3040" <= c <= "\u30ff" for c in source):
            data["ja_source"] = source
        return data

    def get_profile(self, char: str, source: str = "") -> dict | None:
        cache = self.load()
        for key in (self.key(char, source), (char or "").strip()):
            if not key:
                continue
            entry = cache.get(key)
            if not entry:
                continue
            data = self.normalize(entry, char, source)
            if data.get("en_char"):
                return data
        return None

    def get_en_name(self, char: str, source: str = "") -> str | None:
        profile = self.get_profile(char, source)
        if profile:
            return profile.get("en_char") or None
        cache = self.load()
        for key in (self.key(char, source), (char or "").strip()):
            entry = cache.get(key)
            if isinstance(entry, str) and entry:
                return entry
        return None

    def write_profile(self, char: str, source: str, result: dict) -> None:
        if not char or not isinstance(result, dict):
            return
        cache = self.load()
        key = self.key(char, source)
        old = self.normalize(cache.get(key, {}), char, source)
        new = self.normalize(result, char, source)
        merged = {**old, **{k: v for k, v in new.items() if v not in ("", [], None)}}
        if merged.get("en_char"):
            merged["en"] = merged["en_char"]
        if merged.get("alt_char"):
            merged["alt"] = merged["alt_char"]
        cache[key] = merged
        self.save(cache)

    def write_en_name(self, char: str, source: str, en_name: str, alt_chars: list | None = None) -> None:
        if not char or not en_name:
            return
        self.write_profile(char, source, {"en_char": en_name, "alt_char": alt_chars or []})

    def remove(self, char: str, source: str = "") -> int:
        cache = self.load()
        removed = 0
        for key in (self.key(char, source), (char or "").strip()):
            if key and key in cache:
                del cache[key]
                removed += 1
        self.save(cache)
        return removed
