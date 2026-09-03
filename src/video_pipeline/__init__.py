"""Webinar video pipeline: Zoom recording -> trimmed, chaptered Vimeo replay.

The modules here are pure functions over paths and data structures so they can be
driven by three different callers without change: the local validation CLI
(`scripts/debug/video_pipeline_local.py`), the ECS one-shot task, and the API.
"""
