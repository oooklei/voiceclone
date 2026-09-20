# Archive Directory

This directory contains legacy/outdated code and data that are no longer part of the active system.

## Structure

- data_legacy/ - Old test JSON files and prompt text files (obsolete after sidecar rebuild)
- sdk_legacy/ - Old voiceclone-sdk build artifacts and egg-info
- docs_legacy/ - Outdated design documents and plans
- 	ests_legacy/ - Obsolete test files
- scripts_legacy/ - Old deployment scripts and artifacts

## Active System

The active system consists of:
- pp/ - FastAPI business logic (routes, providers, repositories, schemas)
- static/ - Frontend (app.js, index.html, style.css)
- models/ - AI models (whisper, zipvoice)
- sidecar.py - Sidecar entry point
- packaging/sidecar.spec - PyInstaller spec
- oiceclone-desktop/ - Tauri desktop shell
- 	ests/ - Active test suite
- scripts/download_models.ps1 - Model download script
