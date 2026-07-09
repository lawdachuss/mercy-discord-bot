"""
Memory Management Module
========================
Prevents memory leaks and manages caches efficiently.
"""

import logging
from typing import Any, Optional, Dict
from collections import OrderedDict
from datetime import datetime

logger = logging.getLogger('discord.bot.leaderboard.memory')


class MemoryManager:
    """
    LRU cache manager to prevent memory leaks.
    Implements Least Recently Used eviction policy.
    """
    
    def __init__(self, max_cache_size: int = 100):
        """
        Initialize memory manager.
        
        Args:
            max_cache_size: Maximum number of items to cache
        """
        self.max_cache_size = max_cache_size
        self.cache: OrderedDict = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        logger.info(f"MemoryManager initialized with max_cache_size={max_cache_size}")
    
    def add_to_cache(self, key: Any, value: Any) -> None:
        """
        Add item to cache with LRU eviction.
        
        Args:
            key: Cache key
            value: Value to cache
        """
        # If key exists, move to end (most recently used)
        if key in self.cache:
            self.cache.move_to_end(key)
            self.cache[key] = value
            logger.debug(f"Updated cache key: {key}")
            return
        
        # If cache is full, remove oldest item
        if len(self.cache) >= self.max_cache_size:
            oldest_key = next(iter(self.cache))
            self.cache.pop(oldest_key)
            self.evictions += 1
            logger.debug(f"Evicted oldest cache key: {oldest_key} (total evictions: {self.evictions})")
        
        # Add new item
        self.cache[key] = value
        logger.debug(f"Added to cache: {key} (cache size: {len(self.cache)})")
    
    def get_from_cache(self, key: Any) -> Optional[Any]:
        """
        Get item from cache.
        
        Args:
            key: Cache key
            
        Returns:
            Cached value or None if not found
        """
        if key in self.cache:
            # Move to end (most recently used)
            self.cache.move_to_end(key)
            self.hits += 1
            logger.debug(f"Cache hit: {key} (hit rate: {self.get_hit_rate():.1%})")
            return self.cache[key]
        
        self.misses += 1
        logger.debug(f"Cache miss: {key} (hit rate: {self.get_hit_rate():.1%})")
        return None
    
    def remove_from_cache(self, key: Any) -> bool:
        """
        Remove specific item from cache.
        
        Args:
            key: Cache key to remove
            
        Returns:
            True if item was removed, False if not found
        """
        if key in self.cache:
            self.cache.pop(key)
            logger.debug(f"Removed from cache: {key}")
            return True
        return False
    
    def clear_cache(self) -> int:
        """
        Clear all items from cache.
        
        Returns:
            Number of items cleared
        """
        count = len(self.cache)
        self.cache.clear()
        logger.info(f"Cleared cache: {count} items removed")
        return count
    
    def get_cache_size(self) -> int:
        """Get current cache size"""
        return len(self.cache)
    
    def get_hit_rate(self) -> float:
        """
        Calculate cache hit rate.
        
        Returns:
            Hit rate as a float between 0 and 1
        """
        total = self.hits + self.misses
        if total == 0:
            return 0.0
        return self.hits / total
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.
        
        Returns:
            Dictionary with cache stats
        """
        return {
            'size': len(self.cache),
            'max_size': self.max_cache_size,
            'hits': self.hits,
            'misses': self.misses,
            'evictions': self.evictions,
            'hit_rate': self.get_hit_rate()
        }
    
    async def periodic_cleanup(self) -> None:
        """
        Periodic cleanup task.
        Can be extended to implement time-based expiration.
        """
        stats = self.get_stats()
        logger.info(f"Periodic cleanup - Cache stats: {stats}")
        
        # If cache is more than 90% full, log warning
        if stats['size'] > self.max_cache_size * 0.9:
            logger.warning(f"Cache is {stats['size']}/{self.max_cache_size} ({stats['size']/self.max_cache_size:.1%}) full")
    
    def __len__(self) -> int:
        """Return cache size"""
        return len(self.cache)
    
    def __contains__(self, key: Any) -> bool:
        """Check if key is in cache"""
        return key in self.cache
