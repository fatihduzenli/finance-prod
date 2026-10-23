"""``{% page_title %}``: the page's ``title`` block, rendered again for the page header's
default heading (docs/qa/PLAN-012f-approved-design.md D3).

``finance/base.html`` writes the ``title`` block in the tab title; Django renders a block once
per template, so the header's default (a page that does not fill ``{% block header %}``) reads
the most derived ``title`` block from the render's block context and renders its nodes in the
current context: the same text, escaped as the block escapes it. Outside an extending render
(no block context, or no ``title`` block) it renders nothing.
"""

from __future__ import annotations

from django import template
from django.template.context import Context
from django.template.loader_tags import BLOCK_CONTEXT_KEY
from django.utils.safestring import SafeString, mark_safe

register = template.Library()


@register.simple_tag(takes_context=True)
def page_title(context: Context) -> SafeString:
    blocks = context.render_context.get(BLOCK_CONTEXT_KEY)
    block = blocks.get_block("title") if blocks is not None else None
    if block is None:
        return mark_safe("")  # noqa: S308 - the empty string
    # The nodes render as any template text does: variables escaped, markup kept.
    rendered: str = block.nodelist.render(context)
    return mark_safe(rendered.strip())  # noqa: S308
