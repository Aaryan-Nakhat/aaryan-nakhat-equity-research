from equity_research.reports.pdf import _separate_lists, render_html


def test_list_after_paragraph_renders_as_list():
    md = "The split is as follows:\n- **Retail:** ~50%\n- **Wholesale:** ~32%\n"
    html = render_html(md)
    assert "<li><strong>Retail:</strong> ~50%</li>" in html
    assert html.count("<li>") == 2


def test_leaves_tables_code_and_numbers_alone():
    md = "| a | b |\n|---|---|\n| - x | 1 |\n\n```\ntext\n- not a list\n```\nRevenue fell\n-3.5% YoY"
    assert _separate_lists(md) == md


def test_star_bullets_and_numbered_items():
    md = "Shares:\n*   **Cards:** 21%\nSteps:\n1. one\n2. two"
    out = _separate_lists(md)
    assert "Shares:\n\n*   **Cards:**" in out
    assert "Steps:\n\n1. one\n2. two" in out
