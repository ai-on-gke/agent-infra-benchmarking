# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared types for run reports.

A report turns one finished run into one self-contained file. It reads the
run's series back out of Prometheus rather than accumulating state during the
run, so a report can be added without touching the load path.
"""

from dataclasses import dataclass
from html import escape
from typing import Protocol


@dataclass(frozen=True)
class RunContext:
    """What a report knows about the run it is describing."""

    run_id: str
    users: int
    spawn_rate: float
    duration: str
    workload: str
    template: str
    user_class: str
    # Unix seconds bounding the query window.
    started: float
    ended: float
    prometheus_url: str


class Report(Protocol):
    name: str
    filename: str

    def render(self, ctx: RunContext) -> str:
        """Return the file's complete contents, or raise if the run has no
        data for it."""


# Mode-independent chrome: --text-muted is the same value in light and dark,
# so one rendering reads correctly on either background.
INK_MUTED = "#898781"
SERIES_BLUE = "#2a78d6"


def html_page(title: str, subtitle: str, meta: dict[str, str], body: str) -> str:
    rows = "\n".join(
        f"<tr><td>{escape(k)}</td><td>{escape(str(v))}</td></tr>"
        for k, v in meta.items()
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  max-width: 900px; margin: 2rem auto; padding: 0 1rem;
  line-height: 1.6; color: #111; background: #fff;
}}
h1 {{ font-size: 22px; font-weight: 500; margin-bottom: .25rem; }}
p.sub {{ color: #666; margin-top: 0; font-size: 14px; }}
table {{ border-collapse: collapse; font-size: 13px; margin: 1.5rem 0; }}
td {{ border-bottom: 1px solid #e1e0d9; padding: 4px 16px 4px 0; }}
td:first-child {{ color: #666; }}
figure {{ margin: 1.5rem 0; overflow-x: auto; }}
svg {{ max-width: 100%; height: auto; }}
@media (prefers-color-scheme: dark) {{
  body {{ color: #eee; background: #1a1a19; }}
  p.sub, td:first-child {{ color: #aaa; }}
  td {{ border-bottom-color: #2c2c2a; }}
}}
</style>
</head>
<body>
<h1>{escape(title)}</h1>
<p class="sub">{escape(subtitle)}</p>
<table>{rows}</table>
<figure>{body}</figure>
</body>
</html>
"""


def inline_svg(fig) -> str:
    """Render a matplotlib figure as SVG markup ready to embed.

    The XML prolog and DOCTYPE savefig emits are stripped; both are invalid
    partway through an HTML document.
    """
    import io

    buf = io.StringIO()
    fig.savefig(buf, format="svg", bbox_inches="tight", transparent=True)
    svg = buf.getvalue()
    return svg[svg.index("<svg") :]
