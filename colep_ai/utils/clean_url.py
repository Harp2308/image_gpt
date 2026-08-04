from urllib.parse import unquote, urlparse

def clean_link(link: str) -> str:
    path = urlparse(link).path
    return unquote(path)

link = ""

print(clean_link(link))