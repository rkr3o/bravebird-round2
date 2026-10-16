"""
Multi-strategy element resolver.

Finds elements by trying strategies in a fixed order:
  1. role + name    (ARIA role and accessible name)
  2. label          (associated <label> element)
  3. nearby text    (text near the target element)
  4. structure      (positional XPath — tag index within parent)
  5. id or class    (CSS selector — last resort)

Logs which strategy won. Works in two modes:
  - Online: against a live Playwright page
  - Offline: against saved HTML using BeautifulSoup
"""

import re
from dataclasses import dataclass, field
from bs4 import BeautifulSoup


@dataclass
class FieldDescriptor:
    name: str
    role: str = None
    role_name: str = None
    label: str = None
    nearby_text: str = None
    tag: str = "input"
    xpath: str = None         # structural/positional XPath
    css: str = None


@dataclass
class ResolveAttempt:
    strategy: str
    succeeded: bool
    detail: str = ""


@dataclass
class ResolveResult:
    descriptor_name: str
    element: object = None
    strategy_used: str = None
    attempts: list = field(default_factory=list)

    @property
    def found(self):
        return self.element is not None


# ---------------------------------------------------------------------------
# Online resolver — uses a live Playwright page
# ---------------------------------------------------------------------------

async def find_on_page(page, descriptor):
    result = ResolveResult(descriptor_name=descriptor.name)

    strategies = [
        ("role_and_name", _try_role_and_name),
        ("label", _try_label),
        ("nearby_text", _try_nearby_text),
        ("structure", _try_xpath),
        ("id_or_class", _try_css),
    ]

    for strategy_name, strategy_fn in strategies:
        try:
            locator = await strategy_fn(page, descriptor)
            if locator and await locator.count() > 0:
                # verify the element is visible
                if await locator.first.is_visible():
                    result.element = locator.first
                    result.strategy_used = strategy_name
                    result.attempts.append(ResolveAttempt(strategy_name, True))
                    return result
            result.attempts.append(ResolveAttempt(strategy_name, False, "not found"))
        except Exception as err:
            result.attempts.append(ResolveAttempt(strategy_name, False, str(err)[:80]))

    return result


async def _try_role_and_name(page, desc):
    if not desc.role or not desc.role_name:
        return None
    return page.get_by_role(desc.role, name=desc.role_name)


async def _try_label(page, desc):
    if not desc.label:
        return None
    return page.get_by_label(desc.label)


async def _try_nearby_text(page, desc):
    if not desc.nearby_text or not desc.tag:
        return None
    return page.locator(f"text='{desc.nearby_text}'").locator("..").locator(desc.tag)


async def _try_xpath(page, desc):
    if not desc.xpath:
        return None
    return page.locator(f"xpath={desc.xpath}")


async def _try_css(page, desc):
    if not desc.css:
        return None
    return page.locator(desc.css)


# ---------------------------------------------------------------------------
# Offline resolver — uses BeautifulSoup on saved HTML
# ---------------------------------------------------------------------------

def find_in_html(html, descriptor):
    soup = BeautifulSoup(html, "html.parser")
    result = ResolveResult(descriptor_name=descriptor.name)

    # if content is inside an iframe srcdoc, extract it
    iframe = soup.find("iframe")
    if iframe and iframe.get("srcdoc"):
        soup = BeautifulSoup(iframe["srcdoc"], "html.parser")

    strategies = [
        ("role_and_name", _bs_role_and_name),
        ("label", _bs_label),
        ("nearby_text", _bs_nearby_text),
        ("structure", _bs_xpath),
        ("id_or_class", _bs_css),
    ]

    for strategy_name, strategy_fn in strategies:
        try:
            element = strategy_fn(soup, descriptor)
            if element:
                result.element = element
                result.strategy_used = strategy_name
                result.attempts.append(ResolveAttempt(strategy_name, True))
                return result
            result.attempts.append(ResolveAttempt(strategy_name, False, "not found"))
        except Exception as err:
            result.attempts.append(ResolveAttempt(strategy_name, False, str(err)[:80]))

    return result


def _bs_role_and_name(soup, desc):
    if not desc.role or not desc.role_name:
        return None

    tag_map = {
        "textbox": ["input", "textarea"],
        "button": ["button", "input", "summary"],
        "link": ["a"],
        "checkbox": ["input"],
    }
    tags = tag_map.get(desc.role, [desc.tag])

    for tag in tags:
        # aria-label match
        el = soup.find(tag, attrs={"aria-label": re.compile(re.escape(desc.role_name), re.I)})
        if el:
            return el
        # placeholder match
        el = soup.find(tag, attrs={"placeholder": re.compile(re.escape(desc.role_name), re.I)})
        if el:
            return el
        # value match (for submit buttons)
        el = soup.find(tag, attrs={"value": re.compile(re.escape(desc.role_name), re.I)})
        if el:
            return el
        # text content match (for buttons/links)
        for candidate in soup.find_all(tag):
            if desc.role_name.lower() in candidate.get_text().lower():
                return candidate

    return None


def _bs_label(soup, desc):
    if not desc.label:
        return None

    # find label by text
    label_el = None
    for lbl in soup.find_all("label"):
        if desc.label.lower() in lbl.get_text().lower():
            label_el = lbl
            break

    if not label_el:
        return None

    # follow the `for` attribute
    target_id = label_el.get("for")
    if target_id:
        return soup.find(id=target_id)

    # or find the input inside the label
    return label_el.find(desc.tag) or label_el.find("input")


def _bs_nearby_text(soup, desc):
    if not desc.nearby_text or not desc.tag:
        return None

    text_el = soup.find(string=re.compile(re.escape(desc.nearby_text), re.I))
    if not text_el:
        return None

    # walk up looking for the target tag
    parent = text_el.parent
    for _ in range(5):
        if parent is None:
            break
        found = parent.find(desc.tag)
        if found:
            return found
        parent = parent.parent

    return None


def _bs_xpath(soup, desc):
    """Structural match — find by tag index within parent."""
    if not desc.xpath:
        return None

    # simple structural: find the Nth tag of a type in the document
    # parse patterns like "//form//input[1]" or "//div//textarea[1]"
    match = re.search(r'//(\w+)\[(\d+)\]$', desc.xpath)
    if match:
        tag, index = match.group(1), int(match.group(2))
        elements = soup.find_all(tag)
        if 0 < index <= len(elements):
            return elements[index - 1]

    return None


def _bs_css(soup, desc):
    if not desc.css:
        return None
    # handle comma-separated selectors — try each one
    for selector in desc.css.split(","):
        selector = selector.strip()
        if not selector or ":has-text" in selector or ":visible" in selector:
            continue
        try:
            el = soup.select_one(selector)
            if el:
                return el
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def format_resolve_log(results):
    lines = []
    for r in results:
        status = f"FOUND via {r.strategy_used}" if r.found else "NOT FOUND"
        lines.append(f"  [{r.descriptor_name}] {status}")
        for a in r.attempts:
            mark = "+" if a.succeeded else "-"
            detail = f" ({a.detail})" if a.detail else ""
            lines.append(f"    {mark} {a.strategy}{detail}")
    return "\n".join(lines)
