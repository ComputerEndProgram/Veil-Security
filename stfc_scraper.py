"""
STFC.pro player data scraper.

Fetches player info from stfc.pro and stfc.wtf using metadata extraction.
Both TLDs work the same way and return identical data.
"""

import re
import logging
from dataclasses import dataclass
from typing import Optional
import requests
from bs4 import BeautifulSoup

log = logging.getLogger("stfc_scraper")


@dataclass
class PlayerData:
    """Player data extracted from stfc.pro/stfc.wtf"""
    player_id: str
    username: str
    level: int
    alliance_tag: str
    server: int
    
    def __repr__(self):
        return f"PlayerData(id={self.player_id}, name={self.username}, level={self.level}, alliance=[{self.alliance_tag}], server={self.server})"


class STFCProScraper:
    """Scrapes player data from stfc.pro or stfc.wtf."""
    
    BASE_URL_PRO = "https://stfc.pro/players"
    BASE_URL_WTF = "https://stfc.wtf/players"
    TIMEOUT = 10
    
    @staticmethod
    def extract_player_id_from_url(url: str) -> Optional[str]:
        """Extract player ID from a stfc.pro or stfc.wtf URL.
        
        Examples:
            https://stfc.pro/players/2659122580 → 2659122580
            https://stfc.wtf/players/2659122580 → 2659122580
            2659122580 → 2659122580
        """
        # Try to extract from full URL (supports both .pro and .wtf)
        match = re.search(r"stfc\.(pro|wtf)/players/(\d+)", url)
        if match:
            return match.group(2)
        
        # Try to parse as plain ID
        if re.match(r"^\d+$", url.strip()):
            return url.strip()
        
        return None
    
    @staticmethod
    def fetch_player_data(player_id: str) -> Optional[PlayerData]:
        """Fetch player data from stfc.pro or stfc.wtf.
        
        Tries stfc.pro first, then falls back to stfc.wtf if needed.
        
        Returns:
            PlayerData object if successful, None if not found or error.
        """
        # Try both URLs
        for base_url in [STFCProScraper.BASE_URL_PRO, STFCProScraper.BASE_URL_WTF]:
            url = f"{base_url}/{player_id}"
            
            try:
                response = requests.get(
                    url,
                    timeout=STFCProScraper.TIMEOUT,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                )
                response.raise_for_status()
            except requests.RequestException as e:
                log.debug(f"Failed to fetch {url}: {e}")
                continue
            
            # Parse metadata from HTML
            soup = BeautifulSoup(response.content, "html.parser")
            
            # Extract from title and meta tags
            title_tag = soup.find("title")
            meta_desc = soup.find("meta", {"name": "description"})
            
            if not title_tag or not meta_desc:
                log.debug(f"Could not find required metadata for player {player_id} at {base_url}")
                continue
            
            title = title_tag.string or ""
            description = meta_desc.get("content", "")
            
            # Parse title: "DarthHαywire – Level 77 Player - STFC Statistics"
            # Extract username and level
            title_match = re.search(r"^(.+?)\s+–\s+Level\s+(\d+)", title)
            if not title_match:
                log.debug(f"Could not parse title for player {player_id}: {title}")
                continue
            
            username = title_match.group(1)
            level = int(title_match.group(2))
            
            # Parse description: "Stats for DarthHαywire, level 77 of [SITH], server 118."
            desc_match = re.search(r"of\s+\[([^\]]+)\],\s+server\s+(\d+)", description)
            if not desc_match:
                log.debug(f"Could not parse alliance/server from description: {description}")
                continue
            
            alliance_tag = desc_match.group(1)
            server = int(desc_match.group(2))
            
            log.info(f"Successfully fetched player {player_id} from {base_url}")
            return PlayerData(
                player_id=player_id,
                username=username,
                level=level,
                alliance_tag=alliance_tag,
                server=server,
            )
        
        # Both URLs failed
        log.warning(f"Could not fetch player data for {player_id} from stfc.pro or stfc.wtf")
        return None


def format_player_info(player_data: PlayerData) -> str:
    """Format player data for display."""
    return (
        f"**{player_data.username}**\n"
        f"Level: {player_data.level} | Server: {player_data.server} | Alliance: [{player_data.alliance_tag}]"
    )
