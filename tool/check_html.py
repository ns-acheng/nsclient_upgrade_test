import re

html = open(r'c:/mycode/nsclient_upgrade_test/log/11.txt', encoding='utf-8', errors='ignore').read()

rows = re.findall(r'<tr class="zA[^"]*"', html)
print(f'Email rows (tr.zA*): {len(rows)}')
for r in rows[:5]:
    print(' ', r[:120])

# Check wait_for_email_rows selector
rows2 = re.findall(r'<tr[^>]*class="[^"]*\bzA\b', html)
print(f'Email rows (word zA): {len(rows2)}')
