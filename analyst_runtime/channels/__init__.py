"""Chat channels module with plugin architecture."""

from analyst_runtime.channels.base import BaseChannel
from analyst_runtime.channels.manager import ChannelManager

__all__ = ["BaseChannel", "ChannelManager"]
