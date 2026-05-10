import re

def utf16_len(s: str) -> int:
    """Calculate the length of a string in UTF-16 code units."""
    return len(s.encode('utf-16-le')) // 2

PATTERN = re.compile(
    r'(?P<pre>```(?:(?P<lang>[a-zA-Z0-9\+\-\#]+)\n)?(?P<pre_code>.*?)```)|'
    r'(?P<code>`(?P<inline_code>[^`\n]+)`)|'
    r'(?P<bold>\*\*(?P<bold_text>[^*\n]+)\*\*)|'
    r'(?P<italic>\*(?P<italic_text>[^*\n]+)\*)|'
    r'(?P<link>\[(?P<link_text>[^\]\n]+)\]\((?P<link_url>[^)\n]+)\))',
    re.DOTALL
)

def parse_markdown_to_entities(text: str) -> tuple[str, list[dict]]:
    """Parse Markdown text into plain text and a list of Telegram MessageEntity objects."""
    entities = []
    out_text = ""
    last_idx = 0
    
    for m in PATTERN.finditer(text):
        start = m.start()
        
        # Append text before the match
        before = text[last_idx:start]
        out_text += before
        
        # Compute current UTF-16 offset
        offset = utf16_len(out_text)
        
        # Determine match type
        if m.group('pre') is not None:
            inner = m.group('pre_code')
            lang = m.group('lang')
            out_text += inner
            length = utf16_len(inner)
            ent = {"type": "pre", "offset": offset, "length": length}
            if lang:
                ent["language"] = lang
            entities.append(ent)
            
        elif m.group('code') is not None:
            inner = m.group('inline_code')
            out_text += inner
            length = utf16_len(inner)
            entities.append({"type": "code", "offset": offset, "length": length})
            
        elif m.group('bold') is not None:
            inner = m.group('bold_text')
            out_text += inner
            length = utf16_len(inner)
            entities.append({"type": "bold", "offset": offset, "length": length})
            
        elif m.group('italic') is not None:
            inner = m.group('italic_text') or m.group('italic_text2')
            out_text += inner
            length = utf16_len(inner)
            entities.append({"type": "italic", "offset": offset, "length": length})
            
        elif m.group('link') is not None:
            inner = m.group('link_text')
            url = m.group('link_url')
            out_text += inner
            length = utf16_len(inner)
            entities.append({"type": "text_link", "offset": offset, "length": length, "url": url})
            
        last_idx = m.end()
        
    out_text += text[last_idx:]
    return out_text, entities

def split_text_with_entities(text: str, entities: list[dict], max_len: int = 3500) -> list[tuple[str, list[dict]]]:
    """Split text and its associated entities into chunks respecting max_len."""
    if len(text) <= max_len:
        return [(text, entities)]
    
    parts = []
    buf_text = text
    buf_entities = entities
    
    while len(buf_text) > max_len:
        cut = buf_text.rfind("\n", 0, max_len)
        if cut < int(max_len * 0.5):
            cut = buf_text.rfind(" ", 0, max_len)
        if cut < int(max_len * 0.5):
            cut = max_len
            
        chunk_text = buf_text[:cut]
        chunk_utf16_len = utf16_len(chunk_text)
        
        chunk_entities = []
        next_entities = []
        
        for e in buf_entities:
            start = e["offset"]
            end = e["offset"] + e["length"]
            
            if end <= chunk_utf16_len:
                # Fully in chunk
                chunk_entities.append(e)
            elif start >= chunk_utf16_len:
                # Fully in next
                ne = dict(e)
                ne["offset"] = start - chunk_utf16_len
                next_entities.append(ne)
            else:
                # Crosses boundary
                e1 = dict(e)
                e1["length"] = chunk_utf16_len - start
                chunk_entities.append(e1)
                
                e2 = dict(e)
                e2["offset"] = 0
                e2["length"] = end - chunk_utf16_len
                next_entities.append(e2)
                
        parts.append((chunk_text, chunk_entities))
        buf_text = buf_text[cut:]
        buf_entities = next_entities
        
    if buf_text:
        parts.append((buf_text, buf_entities))
        
    return parts
