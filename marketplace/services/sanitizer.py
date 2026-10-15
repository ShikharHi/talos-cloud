import re
import bleach
import markdown

ALLOWED_TAGS = [
    'a', 'p', 'b', 'i', 'strong', 'em', 'code', 'pre', 'ul', 'ol', 'li',
    'h1', 'h2', 'h3', 'h4', 'blockquote', 'hr', 'table', 'thead', 'tbody',
    'tr', 'th', 'td', 'br', 'span'
]

ALLOWED_ATTRIBUTES = {
    'a': ['href', 'title', 'rel', 'target'],
    'code': ['class'],
    'span': ['class']
}

ALLOWED_PROTOCOLS = ['http', 'https', 'talos', 'mailto']

RESERVED_USERNAMES = {
    'admin', 'talos', 'official', 'support', 'system', 'root',
    'api', 'auth', 'marketplace', 'studio', 'explore', 'help',
    'security', 'settings', 'user', 'users', 'bot'
}

def is_username_reserved(username: str) -> bool:
    """Check if a handle or username is reserved by the platform."""
    return username.lower().strip() in RESERVED_USERNAMES

def validate_username_format(username: str) -> bool:
    """Validate username slug format [a-z0-9_-]{3,30}."""
    return bool(re.match(r'^[a-z0-9][a-z0-9_-]{2,29}$', username.lower().strip()))

def sanitize_html(html_content: str) -> str:
    """Sanitize rendered HTML with bleach, restricting allowed tags and protocols."""
    if not html_content:
        return ""
    cleaned = bleach.clean(
        html_content,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True
    )
    # Automatically add rel="nofollow noopener noreferrer" to external links
    cleaned = bleach.linkify(
        cleaned,
        callbacks=[
            lambda attrs, new: attrs.update({(None, 'rel'): 'nofollow noopener noreferrer', (None, 'target'): '_blank'}) or attrs
        ]
    )
    return cleaned

def render_and_sanitize_markdown(md_content: str) -> str:
    """Render markdown to HTML safely using markdown parser and bleach sanitization."""
    if not md_content:
        return ""
    raw_html = markdown.markdown(
        md_content,
        extensions=['fenced_code', 'tables', 'nl2br', 'sane_lists']
    )
    return sanitize_html(raw_html)
