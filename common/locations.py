LOCATION_MAP = {
    "HATF": {
        "location_name": "Henry and the Fox",
        "code": "HATF",
        "search_text": "Henry and the Fox HATF Melbourne CBD",
        "latitude": None,
        "longitude": None,
    },
    "BP": {
        "location_name": "BangPop",
        "code": "BP",
        "search_text": "BangPop BP South Wharf Melbourne",
        "latitude": None,
        "longitude": None,
    },
    "P5": {
        "location_name": "Plus 5",
        "code": "P5",
        "search_text": "Plus 5 P5 South Wharf Melbourne",
        "latitude": None,
        "longitude": None,
    },
    "ALL": {
        "location_name": "All Venues",
        "code": "ALL",
        "search_text": "All Venues Melbourne",
        "latitude": None,
        "longitude": None,
    },
    "UNKNOWN": {
        "location_name": "Unknown",
        "code": "UNKNOWN",
        "search_text": "Unknown",
        "latitude": None,
        "longitude": None,
    },
}


def get_location(location_code: str) -> dict:
    return LOCATION_MAP.get(location_code, LOCATION_MAP["UNKNOWN"])


def get_location_name(location_code: str) -> str:
    return get_location(location_code)["location_name"]


def build_location_object(location_code: str) -> dict:
    loc = get_location(location_code)
    return {
        "code": loc["code"],
        "search_text": loc["search_text"],
        "latitude": loc["latitude"],
        "longitude": loc["longitude"],
    }