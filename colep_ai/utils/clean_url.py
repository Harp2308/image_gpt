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

m_w_link = "https://coleppk.sharepoint.com/:x:/r/sites/AiShoopFloorAssist/Documents/ShopFloor/O01.M%20(Modelo%20-%20produ%C3%A7%C3%A3o)/O01.M007.1%20-%20Manuten%C3%A7%C3%A3o%20Aut%C3%B3noma%20L82.xlsx?d=w6bb9a582bfe44df19e651f0b107e7b3b&csf=1&web=1&e=CFy0oU"

map_link="https://coleppk.sharepoint.com/:x:/r/sites/AiShoopFloorAssist/Documents/ShopFloor/O01.F%20(Fluxogramas)/O01.F001.2%20-%20Fluxograma%20produtivo%20Linha%2020%20e%2022%20-%20Food%20Assembly.xlsx?d=wdaa067d6f8e34c04813c8266b2dac1fa&csf=1&web=1&e=jFdGX8"
print(clean_link(map_link))