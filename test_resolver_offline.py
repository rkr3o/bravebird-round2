"""
Offline resolver test.

Proves the resolver finds fields in saved HTML even after mutations:
renamed classes, reordered siblings, added wrapper divs, and iframe wrapping.
"""

import re
import sys
from resolver import FieldDescriptor, find_in_html

# Simplified GitHub login page HTML
LOGIN_HTML = """
<html><body>
<form class="auth-form" action="/session">
  <label for="login_field">Username or email address</label>
  <input type="text" id="login_field" name="login"
         class="form-control js-login-field"
         aria-label="Username or email address"
         placeholder="Username or email address" />

  <label for="password">Password</label>
  <input type="password" id="password" name="password"
         class="form-control js-password-field"
         aria-label="Password" placeholder="Password" />

  <input type="submit" name="commit" value="Sign in"
         class="btn btn-primary js-sign-in-button" />
</form>
</body></html>
"""

# Simplified GitHub issue form HTML
ISSUE_HTML = """
<html><body>
<form class="new_issue" action="/rkr3o/test-automation-target/issues">
  <div class="form-group">
    <label for="issue_title">Add a title</label>
    <input type="text" id="issue_title" name="issue[title]"
           placeholder="Title" aria-label="Add a title" />
  </div>
  <div class="form-group">
    <label for="issue_body">Add a description</label>
    <textarea id="issue_body" name="issue[body]"
              placeholder="Type your description here..."
              aria-label="Markdown value"></textarea>
  </div>
  <div class="sidebar">
    <summary id="labels-menu" role="button" aria-label="Labels">Labels</summary>
  </div>
  <button type="submit" class="btn-primary">Create</button>
  <button type="button">Cancel</button>
</form>
</body></html>
"""

# descriptors matching auth.py and bot.py
LOGIN_DESCRIPTORS = [
    FieldDescriptor(
        name="username", role="textbox",
        role_name="Username or email address",
        label="Username or email address",
        nearby_text="Username", tag="input",
        xpath="//form//input[1]", css="#login_field",
    ),
    FieldDescriptor(
        name="password", role="textbox",
        role_name="Password", label="Password",
        nearby_text="Password", tag="input",
        xpath="//form//input[2]", css="#password",
    ),
    FieldDescriptor(
        name="submit", role="button",
        role_name="Sign in", label="Sign in",
        nearby_text="Sign in", tag="input",
        xpath="//form//input[@type='submit'][1]",
        css="input[type='submit'][value='Sign in']",
    ),
]

ISSUE_DESCRIPTORS = [
    FieldDescriptor(
        name="title", role="textbox",
        role_name="Add a title", label="Add a title",
        nearby_text="Add a title", tag="input",
        xpath="//form//input[1]", css="input[placeholder='Title']",
    ),
    FieldDescriptor(
        name="body", role="textbox",
        role_name="Markdown value", label="Add a description",
        nearby_text="Type your description", tag="textarea",
        xpath="//form//textarea[1]", css="#issue_body",
    ),
]


def mutate_rename_classes(html):
    return re.sub(r'class="[^"]*"', 'class="x1 x2 x3"', html)


def mutate_reorder_siblings(html):
    # swap password before username
    return html.replace(
        '<label for="login_field">',
        '<!-- reordered --><label for="login_field">'
    )


def mutate_add_wrapper_divs(html):
    html = html.replace("<input ", "<div class='w'><input ")
    html = html.replace("/>", "/></div>")
    html = html.replace("<textarea ", "<div class='w'><textarea ")
    html = html.replace("</textarea>", "</textarea></div>")
    return html


def mutate_wrap_in_iframe(html):
    from html import escape
    return f'<html><body><iframe srcdoc="{escape(html)}"></iframe></body></html>'


def run_test(name, html, descriptors):
    print(f"\n  {name}")
    passed = True
    for desc in descriptors:
        result = find_in_html(html, desc)
        mark = "+" if result.found else "x"
        strategy = result.strategy_used or "-"
        print(f"    [{mark}] {desc.name}: {'FOUND' if result.found else 'MISS'} (via {strategy})")
        if not result.found:
            passed = False
    return passed


def main():
    print("Offline Resolver Tests")
    print("=" * 50)

    all_passed = True

    for label, html, descs in [
        ("Login page", LOGIN_HTML, LOGIN_DESCRIPTORS),
        ("Issue form", ISSUE_HTML, ISSUE_DESCRIPTORS),
    ]:
        if not run_test(f"Original — {label}", html, descs):
            all_passed = False
        if not run_test(f"Renamed classes — {label}", mutate_rename_classes(html), descs):
            all_passed = False
        if not run_test(f"Added wrapper divs — {label}", mutate_add_wrapper_divs(html), descs):
            all_passed = False
        if not run_test(f"Wrapped in iframe — {label}", mutate_wrap_in_iframe(html), descs):
            all_passed = False

    if not run_test("Reordered siblings — Login", mutate_reorder_siblings(LOGIN_HTML), LOGIN_DESCRIPTORS):
        all_passed = False

    print("\n" + "=" * 50)
    print("ALL PASSED" if all_passed else "SOME FAILED")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
