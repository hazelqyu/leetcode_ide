#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any
from urllib import error, request


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = _strip_wrapping_quotes(value.strip())
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()
LEETCODE_BASE = os.environ.get("LEETCODE_BASE", "https://leetcode.com")
WORKSPACE_DIR = Path(os.environ.get("LCIDE_WORKSPACE", "problems"))
NEETCODE150_SOURCE = os.environ.get(
    "NEETCODE150_SOURCE",
    "https://raw.githubusercontent.com/neetcode-gh/leetcode/main/.problemSiteData.json",
)
ROADMAP_CACHE_PATH = Path(os.environ.get("LCIDE_ROADMAP_CACHE", ".cache/neetcode150.json"))
ROADMAP_STATIC_PATH = Path(os.environ.get("LCIDE_ROADMAP_JSON", "data/neetcode150.json"))


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


@dataclass
class LeetCodeAuth:
    session: str
    csrf: str


@dataclass
class RoadmapProblem:
    category: str
    title: str
    slug: str
    difficulty: str
    url: str


class LeetCodeClient:
    def __init__(self, base_url: str, auth: LeetCodeAuth | None = None):
        self.base_url = base_url.rstrip("/")
        self.auth = auth

    def _request(
        self,
        path: str,
        method: str = "GET",
        data: dict[str, Any] | None = None,
        auth_required: bool = False,
    ) -> dict[str, Any]:
        if auth_required and not self.auth:
            raise RuntimeError("Auth required. Set LEETCODE_SESSION and LEETCODE_CSRFTOKEN.")

        body: bytes | None = None
        headers = {
            "Content-Type": "application/json",
            "Referer": f"{self.base_url}/",
            "User-Agent": "lcide/0.1",
        }

        if self.auth:
            headers["Cookie"] = (
                f"LEETCODE_SESSION={self.auth.session}; csrftoken={self.auth.csrf}"
            )
            headers["x-csrftoken"] = self.auth.csrf

        if data is not None:
            body = json.dumps(data).encode("utf-8")

        url = f"{self.base_url}{path}"
        req = request.Request(url, data=body, headers=headers, method=method)

        try:
            with request.urlopen(req) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc
        except error.URLError as exc:
            raise RuntimeError(
                f"Network error for {url}: {exc.reason}. "
                "Check internet access and LEETCODE_BASE."
            ) from exc

    def question_data(self, slug: str) -> dict[str, Any]:
        query = """
        query questionData($titleSlug: String!) {
          question(titleSlug: $titleSlug) {
            questionId
            questionFrontendId
            title
            titleSlug
            content
            difficulty
            likes
            dislikes
            exampleTestcases
            codeSnippets {
              lang
              langSlug
              code
            }
          }
        }
        """
        payload = {
            "query": query,
            "variables": {"titleSlug": slug},
            "operationName": "questionData",
        }
        data = self._request("/graphql/", method="POST", data=payload)
        question = data.get("data", {}).get("question")
        if not question:
            raise RuntimeError(f"Could not find problem slug '{slug}'.")
        return question

    def run_code(self, slug: str, code: str, lang: str, test_input: str) -> dict[str, Any]:
        payload = {
            "lang": lang,
            "question_id": self._question_id(slug),
            "typed_code": code,
            "data_input": test_input,
        }
        result = self._request(
            f"/problems/{slug}/interpret_solution/",
            method="POST",
            data=payload,
            auth_required=True,
        )
        interpret_id = result.get("interpret_id")
        if not interpret_id:
            return result

        for _ in range(30):
            time.sleep(1)
            check = self._request(
                f"/submissions/detail/{interpret_id}/check/",
                auth_required=True,
            )
            state = check.get("state")
            if state == "SUCCESS":
                return check
        raise RuntimeError("Timed out waiting for run result.")

    def submit_code(self, slug: str, code: str, lang: str) -> dict[str, Any]:
        payload = {
            "lang": lang,
            "question_id": self._question_id(slug),
            "typed_code": code,
        }
        result = self._request(
            f"/problems/{slug}/submit/",
            method="POST",
            data=payload,
            auth_required=True,
        )
        submission_id = result.get("submission_id")
        if not submission_id:
            return result

        for _ in range(60):
            time.sleep(1)
            check = self._request(
                f"/submissions/detail/{submission_id}/check/",
                auth_required=True,
            )
            state = check.get("state")
            if state == "SUCCESS":
                return check
        raise RuntimeError("Timed out waiting for submission result.")

    def _question_id(self, slug: str) -> str:
        question = self.question_data(slug)
        qid = question.get("questionId")
        if not qid:
            raise RuntimeError(f"questionId missing for '{slug}'.")
        return str(qid)


def _sanitize_markdown(html_text: str) -> str:
    # Very light HTML cleanup so prompt is readable in Markdown.
    text = re.sub(r"<pre>", "\n```\n", html_text)
    text = re.sub(r"</pre>", "\n```\n", text)
    text = re.sub(r"<code>", "`", text)
    text = re.sub(r"</code>", "`", text)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip() + "\n"


def _problem_dir(frontend_id: str, slug: str) -> Path:
    safe_id = frontend_id.zfill(4)
    return WORKSPACE_DIR / f"{safe_id}-{slug}"


def _pull_problem_by_slug(slug: str) -> tuple[str, str, Path]:
    client = LeetCodeClient(LEETCODE_BASE)
    question = client.question_data(slug)

    frontend_id = str(question["questionFrontendId"])
    fetched_slug = question["titleSlug"]
    out_dir = _problem_dir(frontend_id, fetched_slug)
    out_dir.mkdir(parents=True, exist_ok=True)

    snippets = question.get("codeSnippets", [])
    py_snippet = next((s for s in snippets if s.get("langSlug") == "python3"), None)
    starter = py_snippet["code"] if py_snippet else "class Solution:\n    pass\n"

    prompt = _sanitize_markdown(question.get("content", ""))
    prompt_header = (
        f"# {frontend_id}. {question['title']}\n\n"
        f"- Difficulty: {question.get('difficulty', 'Unknown')}\n"
        f"- Likes: {question.get('likes', 0)}\n"
        f"- Dislikes: {question.get('dislikes', 0)}\n"
        f"- URL: {LEETCODE_BASE}/problems/{fetched_slug}/\n\n"
    )

    prompt_path = out_dir / "prompt.md"
    solution_path = out_dir / "solution.py"
    tests_path = out_dir / "example_testcases.txt"
    meta_path = out_dir / "meta.json"

    prompt_path.write_text(prompt_header + prompt, encoding="utf-8")
    if not solution_path.exists():
        solution_path.write_text(starter.rstrip() + "\n", encoding="utf-8")

    example_testcases = question.get("exampleTestcases") or ""
    tests_path.write_text(example_testcases.strip() + "\n", encoding="utf-8")

    meta = {
        "slug": fetched_slug,
        "questionId": question.get("questionId"),
        "questionFrontendId": frontend_id,
        "title": question.get("title"),
        "lang": "python3",
        "pulledAtEpoch": int(time.time()),
    }
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return frontend_id, str(question["title"]), out_dir


def cmd_pull(args: argparse.Namespace) -> int:
    frontend_id, title, out_dir = _pull_problem_by_slug(args.slug)
    print(f"Pulled {frontend_id}. {title}")
    print(f"Directory: {out_dir}")
    return 0


def _fetch_text(url: str) -> str:
    req = request.Request(url, headers={"User-Agent": "lcide/0.1"})
    try:
        with request.urlopen(req) as resp:
            return resp.read().decode("utf-8")
    except error.URLError as exc:
        raise RuntimeError(f"Network error for {url}: {exc.reason}") from exc


def _parse_roadmap_markdown(markdown: str) -> list[RoadmapProblem]:
    text = re.sub(r"\s+##\s+", "\n## ", markdown)
    text = re.sub(r"\s+(\d+\.\s+\[)", r"\n\1", text)
    current_category = ""
    problems: list[RoadmapProblem] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("## "):
            current_category = line[3:].strip()
            continue
        match = re.match(
            r"^\d+\.\s+\[([^\]]+)\]\((https://leetcode\.com/problems/([^/\)]+)/*)\)\s+\((Easy|Medium|Hard)\)$",
            line,
        )
        if match:
            title, url, slug, difficulty = match.groups()
            problems.append(
                RoadmapProblem(
                    category=current_category or "Uncategorized",
                    title=title.strip(),
                    slug=slug.strip(),
                    difficulty=difficulty.strip(),
                    url=url.strip(),
                )
            )
    if not problems:
        raise RuntimeError("Could not parse roadmap source.")
    return problems


def _parse_roadmap_json(raw_json: str) -> list[RoadmapProblem]:
    payload = json.loads(raw_json)
    if isinstance(payload, dict):
        items = payload.get("problems")
        if not isinstance(items, list):
            # Fallback: some versions may store entries at top-level arrays inside values.
            items = next((v for v in payload.values() if isinstance(v, list)), None)
    elif isinstance(payload, list):
        items = payload
    else:
        items = None

    if not isinstance(items, list):
        raise RuntimeError("Unexpected roadmap JSON format.")

    problems: list[RoadmapProblem] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if not item.get("neetcode150"):
            continue
        title = str(item.get("problem", "")).strip()
        slug = str(item.get("link", "")).strip().strip("/")
        category = str(item.get("pattern", "")).strip() or "Uncategorized"
        difficulty = str(item.get("difficulty", "")).strip() or "Unknown"
        if not title or not slug:
            continue
        problems.append(
            RoadmapProblem(
                category=category,
                title=title,
                slug=slug,
                difficulty=difficulty,
                url=f"https://leetcode.com/problems/{slug}/",
            )
        )

    if not problems:
        raise RuntimeError("Roadmap JSON parsed but no NeetCode 150 problems were found.")
    return problems


def _save_roadmap_cache(problems: list[RoadmapProblem]) -> None:
    ROADMAP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = [p.__dict__ for p in problems]
    ROADMAP_CACHE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _save_roadmap_static(problems: list[RoadmapProblem]) -> None:
    ROADMAP_STATIC_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = [p.__dict__ for p in problems]
    ROADMAP_STATIC_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _load_roadmap_cache(path: Path = ROADMAP_CACHE_PATH) -> list[RoadmapProblem]:
    if not path.exists():
        raise RuntimeError(f"Roadmap file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    problems = [RoadmapProblem(**item) for item in payload]
    if not problems:
        raise RuntimeError(f"Roadmap file is empty: {path}")
    return problems


def _load_roadmap(force_refresh: bool = False) -> list[RoadmapProblem]:
    if not force_refresh:
        try:
            return _load_roadmap_cache(ROADMAP_STATIC_PATH)
        except RuntimeError:
            try:
                return _load_roadmap_cache(ROADMAP_CACHE_PATH)
            except RuntimeError:
                pass

    try:
        raw = _fetch_text(NEETCODE150_SOURCE)
        if NEETCODE150_SOURCE.endswith(".json"):
            problems = _parse_roadmap_json(raw)
        else:
            problems = _parse_roadmap_markdown(raw)
        _save_roadmap_static(problems)
        _save_roadmap_cache(problems)
        return problems
    except RuntimeError:
        if ROADMAP_STATIC_PATH.exists():
            return _load_roadmap_cache(ROADMAP_STATIC_PATH)
        if ROADMAP_CACHE_PATH.exists():
            return _load_roadmap_cache(ROADMAP_CACHE_PATH)
        raise


def _match_category(categories: list[str], requested: str) -> str:
    direct = {c.lower(): c for c in categories}
    if requested.lower() in direct:
        return direct[requested.lower()]

    for category in categories:
        if requested.lower() in category.lower():
            return category
    raise RuntimeError(f"Category '{requested}' not found.")


def cmd_roadmap_sync(args: argparse.Namespace) -> int:
    problems = _load_roadmap(force_refresh=True)
    categories = sorted({p.category for p in problems})
    print(f"Synced roadmap with {len(problems)} problems across {len(categories)} categories.")
    print(f"JSON: {ROADMAP_STATIC_PATH}")
    return 0


def cmd_roadmap_categories(args: argparse.Namespace) -> int:
    problems = _load_roadmap(force_refresh=args.refresh)
    counts: dict[str, int] = {}
    for p in problems:
        counts[p.category] = counts.get(p.category, 0) + 1

    for idx, category in enumerate(sorted(counts), start=1):
        print(f"{idx:>2}. {category} ({counts[category]})")
    return 0


def cmd_roadmap_problems(args: argparse.Namespace) -> int:
    problems = _load_roadmap(force_refresh=args.refresh)
    categories = sorted({p.category for p in problems})
    category = _match_category(categories, args.category)
    selected = [p for p in problems if p.category == category]
    for idx, p in enumerate(selected, start=1):
        print(f"{idx:>2}. [{p.difficulty:<6}] {p.title} ({p.slug})")
    return 0


def cmd_roadmap_pull(args: argparse.Namespace) -> int:
    problems = _load_roadmap(force_refresh=args.refresh)

    selected_problem: RoadmapProblem | None = None
    if args.title:
        matches = [p for p in problems if p.title.lower() == args.title.lower()]
        if not matches:
            matches = [p for p in problems if args.title.lower() in p.title.lower()]
        if not matches:
            raise RuntimeError(f"No roadmap problem found matching title '{args.title}'.")
        if len(matches) > 1:
            choices = ", ".join(sorted({m.title for m in matches})[:5])
            raise RuntimeError(f"Title is ambiguous. Matches include: {choices}")
        selected_problem = matches[0]
    else:
        if not args.category:
            raise RuntimeError("Provide --category when using --index.")
        categories = sorted({p.category for p in problems})
        category = _match_category(categories, args.category)
        in_category = [p for p in problems if p.category == category]
        if args.index < 1 or args.index > len(in_category):
            raise RuntimeError(
                f"Index {args.index} out of range for category '{category}' (1..{len(in_category)})."
            )
        selected_problem = in_category[args.index - 1]

    frontend_id, title, out_dir = _pull_problem_by_slug(selected_problem.slug)
    print(f"Roadmap category: {selected_problem.category}")
    print(f"Pulled {frontend_id}. {title}")
    print(f"Directory: {out_dir}")
    return 0


def _load_problem(slug: str) -> tuple[dict[str, Any], Path]:
    candidates = sorted(WORKSPACE_DIR.glob(f"*-{slug}"))
    if not candidates:
        raise RuntimeError(f"Problem '{slug}' not found locally. Run pull first.")
    problem_dir = candidates[-1]
    meta_path = problem_dir / "meta.json"
    if not meta_path.exists():
        raise RuntimeError(f"Missing metadata: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return meta, problem_dir


def _read_auth() -> LeetCodeAuth:
    session = os.environ.get("LEETCODE_SESSION")
    csrf = os.environ.get("LEETCODE_CSRFTOKEN")
    if not session or not csrf:
        raise RuntimeError(
            "Missing auth env vars. Set LEETCODE_SESSION and LEETCODE_CSRFTOKEN "
            "in environment or .env file."
        )
    return LeetCodeAuth(session=session, csrf=csrf)


def cmd_run(args: argparse.Namespace) -> int:
    meta, problem_dir = _load_problem(args.slug)
    code = (problem_dir / "solution.py").read_text(encoding="utf-8")

    if args.input:
        test_input = args.input
    else:
        test_input = (problem_dir / "example_testcases.txt").read_text(encoding="utf-8").strip()

    if not test_input:
        raise RuntimeError("No test input provided. Pass --input or edit example_testcases.txt")

    client = LeetCodeClient(LEETCODE_BASE, auth=_read_auth())
    result = client.run_code(meta["slug"], code, meta.get("lang", "python3"), test_input)

    print(json.dumps(_compact_run_result(result), indent=2))
    return 0


def _compact_run_result(result: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "status_msg",
        "status_runtime",
        "runtime_percentile",
        "status_memory",
        "memory_percentile",
        "compile_error",
        "runtime_error",
        "last_testcase",
        "expected_output",
        "code_output",
        "std_output",
        "total_correct",
        "total_testcases",
    ]
    return {k: result.get(k) for k in keys if result.get(k) is not None}


def cmd_submit(args: argparse.Namespace) -> int:
    meta, problem_dir = _load_problem(args.slug)
    code = (problem_dir / "solution.py").read_text(encoding="utf-8")

    client = LeetCodeClient(LEETCODE_BASE, auth=_read_auth())
    result = client.submit_code(meta["slug"], code, meta.get("lang", "python3"))

    print(json.dumps(_compact_submit_result(result), indent=2))
    return 0


def _compact_submit_result(result: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "status_msg",
        "status_runtime",
        "runtime_percentile",
        "status_memory",
        "memory_percentile",
        "compare_result",
        "pretty_lang",
        "total_correct",
        "total_testcases",
        "full_compile_error",
        "full_runtime_error",
    ]
    compact = {k: result.get(k) for k in keys if result.get(k) is not None}
    if "compare_result" in compact and isinstance(compact["compare_result"], str):
        compact["compare_result"] = compact["compare_result"][:120]
    return compact


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LeetCode IDE helper CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pull = sub.add_parser("pull", help="Download a problem into local workspace")
    pull.add_argument("slug", help="Problem slug, e.g. two-sum")
    pull.set_defaults(func=cmd_pull)

    run = sub.add_parser("run", help="Run solution against test input using LeetCode")
    run.add_argument("slug", help="Problem slug, e.g. two-sum")
    run.add_argument("--input", help="Custom test input text")
    run.set_defaults(func=cmd_run)

    submit = sub.add_parser("submit", help="Submit current local solution")
    submit.add_argument("slug", help="Problem slug, e.g. two-sum")
    submit.set_defaults(func=cmd_submit)

    roadmap = sub.add_parser("roadmap", help="Browse and pull NeetCode roadmap problems")
    roadmap_sub = roadmap.add_subparsers(dest="roadmap_cmd", required=True)

    roadmap_sync = roadmap_sub.add_parser("sync", help="Refresh roadmap dataset cache")
    roadmap_sync.set_defaults(func=cmd_roadmap_sync)

    roadmap_categories = roadmap_sub.add_parser("categories", help="List roadmap categories")
    roadmap_categories.add_argument(
        "--refresh", action="store_true", help="Refresh roadmap data from remote source"
    )
    roadmap_categories.set_defaults(func=cmd_roadmap_categories)

    roadmap_problems = roadmap_sub.add_parser(
        "problems", help="List problems inside a roadmap category"
    )
    roadmap_problems.add_argument("--category", required=True, help="Category name")
    roadmap_problems.add_argument(
        "--refresh", action="store_true", help="Refresh roadmap data from remote source"
    )
    roadmap_problems.set_defaults(func=cmd_roadmap_problems)

    roadmap_pull = roadmap_sub.add_parser(
        "pull",
        help="Pull a roadmap problem by category index or by title",
    )
    roadmap_pull.add_argument("--category", help="Category name for index-based pull")
    roadmap_pull.add_argument("--index", type=int, default=1, help="1-based problem index")
    roadmap_pull.add_argument("--title", help="Problem title (exact or partial match)")
    roadmap_pull.add_argument(
        "--refresh", action="store_true", help="Refresh roadmap data from remote source"
    )
    roadmap_pull.set_defaults(func=cmd_roadmap_pull)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except RuntimeError as exc:
        eprint(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
