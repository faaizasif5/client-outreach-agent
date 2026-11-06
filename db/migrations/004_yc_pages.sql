-- 004: the YC scraper reads more than one public results page (ycombinator.com board + Work at a Startup).
ALTER TABLE yc_health ADD COLUMN page TEXT;
ALTER TABLE yc_repairs ADD COLUMN page TEXT;
