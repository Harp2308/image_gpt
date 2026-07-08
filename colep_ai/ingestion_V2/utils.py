import re
import unicodedata


def normalize_filename(name: str) -> str:
    """
    Normalizes a filename/stem to a filesystem-safe, deterministic id.
    'O01.O119.1 - Localização dos pontos de lubrificação e processo de limpeza L13'
    -> 'O01_O119_1_Localizacao_dos_pontos_de_lubrificacao_e_processo_de_limpeza_L13'
    """
    # strip accents (ç -> c, ã -> a, õ -> o)
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))

    # replace anything not alnum with underscore
    name = re.sub(r"[^A-Za-z0-9]+", "_", name)

    # collapse multiple underscores, strip leading/trailing
    name = re.sub(r"_+", "_", name).strip("_")

    return name


# print(normalize_filename("O01.O119.1 - Localização dos pontos de lubrificação e processo de limpeza L13.xlsx"))