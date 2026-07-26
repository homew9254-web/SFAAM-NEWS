#!/bin/bash
# ============================================
# SFAAM NEWS V29 - Start Script
# Railway, Render, ya kisi bhi cloud pe chalega
# Yeh script PORT variable ko properly handle karti hai
# ============================================

set -e

# PORT variable check - Railway auto set karta hai
# Agar PORT set nahi hai toh 8000 use karo
PORT=${PORT:-8000}

echo "============================================"
echo "  SFAAM NEWS V29 - Starting Server"
echo "  HOST: 0.0.0.0"
echo "  PORT: $PORT"
echo "============================================"

# Start uvicorn with resolved PORT value
exec uvicorn main:app --host 0.0.0.0 --port "$PORT"
