"""
Patch discover.py to parse tab-separated rows instead of td cells.
The Dallas County portal renders rows with tabs, not <td> elements.
"""
import re

with open('discover.py', 'r') as f:
    code = f.read()

# Find the forEach block and replace it
old_pattern = r"document\.querySelectorAll\('table tbody tr'\)\.forEach\(row=>\{.*?\}\);"
match = re.search(old_pattern, code, re.DOTALL)

if match:
    old_block = match.group()
    new_block = """document.querySelectorAll('table tbody tr').forEach(row=>{
                    const link=row.querySelector('a');
                    const text=row.innerText||'';
                    const parts=text.split(/[\\t\\n]/).map(s=>s.trim()).filter(s=>s);
                    const cn=parts.find(p=>/^TX-\\d{2}-\\d{5}$/.test(p));
                    if(!cn)return;
                    const i=parts.indexOf(cn);
                    out.push({caseNumber:cn,
                        fileDate:parts[i+1]||'',
                        status:parts[i+3]||'',
                        court:parts[i+4]||'',
                        partyName:parts[i+5]||'',
                        href:link?link.href:''});
                });"""
    code = code.replace(old_block, new_block)
    print("Fixed - tab-separated parsing applied")
else:
    print("Block not found")

with open('discover.py', 'w') as f:
    f.write(code)
print("Done")
