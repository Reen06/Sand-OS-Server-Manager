"""
title: NAS Files
author: SandOS
description: List and read files in your own personal NAS storage — the same files visible in Filebrowser/FreeCAD/ParaView for your account. Only ever accesses YOUR own home directory, never another user's.
version: 1.0.0
"""

import os

# The whole fleet NAS export is mounted here (Mount(name="nas", path="/nas",
# scope="root", storage="nfs") in registry.py) — same pattern Nextcloud
# already uses for the identical reason: this is ONE shared container for
# every Hub user, so there's no single per-user mount that would work.
# "users" matches config.py's NAS_USERS_SUBPATH ("users/{username}").
NAS_USERS_ROOT = "/nas/users"


class Tools:
    def __init__(self):
        pass

    def _user_dir(self, __user__: dict) -> str:
        """Resolve the CURRENT authenticated user's own NAS home — never
        anything the model or a function-call argument can override. __user__
        is injected by Open WebUI's own backend from the real session, not
        something the caller controls."""
        username = (__user__ or {}).get("name") or (__user__ or {}).get("email")
        if not username or "/" in username or "\\" in username or username in (".", ".."):
            raise ValueError("Could not determine your NAS username from the current session.")

        root = os.path.realpath(NAS_USERS_ROOT)
        target = os.path.realpath(os.path.join(root, username))
        # Defends against a crafted username (shouldn't be possible via the
        # trusted SSO header, but this is the actual security boundary, not
        # the header) AND a malicious symlink under the NAS export pointing
        # outside this user's own directory.
        if target != root and not target.startswith(root + os.sep):
            raise ValueError("Refusing to access a path outside your NAS home.")
        if not os.path.isdir(target):
            raise ValueError(
                f"No NAS home found for user '{username}' yet — it's created "
                "the first time you use any SandOS app that touches your files."
            )
        return target

    def _safe_path(self, __user__: dict, relative_path: str) -> str:
        """Resolve relative_path against the user's own NAS home, refusing
        anything that escapes it (../, absolute paths, symlink tricks)."""
        base = self._user_dir(__user__)
        relative_path = (relative_path or "").strip().lstrip("/")
        target = os.path.realpath(os.path.join(base, relative_path))
        if target != base and not target.startswith(base + os.sep):
            raise ValueError(
                "That path would escape your NAS home directory — not allowed."
            )
        return target

    def list_my_files(self, path: str = "", __user__: dict = {}) -> str:
        """
        List files and folders in your personal NAS storage.

        :param path: Subfolder path relative to the root of your NAS home. Leave empty to list the top level of your own storage.
        :return: A listing of files and folders at that location, or an error message.
        """
        try:
            target = self._safe_path(__user__, path)
        except ValueError as e:
            return f"Error: {e}"

        if not os.path.isdir(target):
            return f"Error: '{path}' is a file, not a directory. Use read_my_file instead."

        try:
            names = sorted(os.listdir(target))
        except Exception as e:
            return f"Error listing directory: {e}"

        if not names:
            return "(empty directory)"

        lines = []
        for name in names:
            full = os.path.join(target, name)
            try:
                if os.path.isdir(full):
                    lines.append(f"[dir]  {name}")
                else:
                    size = os.path.getsize(full)
                    lines.append(f"[file] {name} ({size:,} bytes)")
            except OSError:
                lines.append(f"[?]    {name} (inaccessible)")
        return "\n".join(lines)

    def read_my_file(self, path: str, max_chars: int = 20000, __user__: dict = {}) -> str:
        """
        Read the text contents of a file in your personal NAS storage.

        :param path: File path relative to the root of your NAS home (e.g. "notes/todo.txt").
        :param max_chars: Maximum number of characters to return, to avoid dumping huge files (default 20000).
        :return: The file's text content (truncated if it exceeds max_chars), or an error message.
        """
        try:
            target = self._safe_path(__user__, path)
        except ValueError as e:
            return f"Error: {e}"

        if not os.path.isfile(target):
            return f"Error: '{path}' is not a file (it may be a directory, or not exist)."

        try:
            size = os.path.getsize(target)
        except OSError as e:
            return f"Error: {e}"
        # Binary/huge-file guard: reading an arbitrary large or non-text file
        # as UTF-8 text isn't useful to a model and can eat a lot of context.
        if size > 5_000_000:
            return (
                f"Error: '{path}' is {size:,} bytes — too large to read as text. "
                "Try a smaller file, or ask to list a subdirectory instead."
            )

        try:
            with open(target, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(max_chars + 1)
        except Exception as e:
            return f"Error reading file: {e}"

        if len(content) > max_chars:
            content = content[:max_chars] + f"\n...[truncated, file continues past {max_chars} characters]"
        return content
