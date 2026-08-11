from urllib.parse import unquote, urlparse, parse_qs

def clean_link(link: str) -> str:
    parsed = urlparse(link)

    # Decode the path
    path = unquote(parsed.path)

    # Parse and decode query parameters
    query = parse_qs(parsed.query)

    if "file" in query:
        filename = query["file"][0]
        return f"{path}/{filename}"

    return path

map_link=""
print(clean_link(map_link))