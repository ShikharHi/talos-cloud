from typing import Optional, Dict, Any

def generate_plugin_jsonld(
    name: str,
    description: str,
    url: str,
    builder_name: str,
    rating_avg: Optional[float] = None,
    rating_count: int = 0
) -> Dict[str, Any]:
    """
    Generate Schema.org SoftwareApplication JSON-LD.
    CONDITIONAL: aggregateRating emitted ONLY when rating_count > 0.
    """
    data: Dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": name,
        "applicationCategory": "DeveloperApplication",
        "operatingSystem": "All",
        "description": description,
        "url": url,
        "author": {
            "@type": "Person",
            "name": builder_name
        }
    }
    if rating_count > 0 and rating_avg is not None:
        data["aggregateRating"] = {
            "@type": "AggregateRating",
            "ratingValue": round(rating_avg, 1),
            "reviewCount": rating_count,
            "bestRating": "5",
            "worstRating": "1"
        }
    return data

def generate_builder_jsonld(
    name: str,
    username: str,
    bio: str,
    url: str
) -> Dict[str, Any]:
    """Generate Schema.org Person JSON-LD for Builders."""
    return {
        "@context": "https://schema.org",
        "@type": "Person",
        "name": name,
        "alternateName": f"@{username}",
        "description": bio,
        "url": url
    }
