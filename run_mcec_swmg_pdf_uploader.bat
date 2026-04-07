@echo off
cd /d "C:\Users\sam\My Drive\HOSPITALITY 1\MARKETING\Marketing_2021\__Scripts__\Web_Scrapers_Pi"

set PYTHONPATH=%cd%
set S3_BUCKET=event-scrape-data
set AWS_PROFILE=raspberry-pi-scraper

python scrapers\mcec_swmg_pdf_uploader_app.py

pause