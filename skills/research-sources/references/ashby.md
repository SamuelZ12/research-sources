# Ashby

`ashby` uses the board name in the employer's careers link.

- **Search** returns listed roles in API order. Filter by title words, team or department, location, and last publication date (UTC). `--location` matches primary and secondary location labels, cities, regions and countries that the board supplies. A matching address does not establish who is eligible to work there. `text` gives one line of available facts: locations, workplace type, employment type, team and employer-published pay.
- **Read** puts those facts above the description in `text`. The description is Markdown, with links and embedded media as `[text](URL)`.

A board is a snapshot of current postings. It has no hiring history and no applicant counts.
