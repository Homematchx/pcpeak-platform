import re

with open('discover.py','r') as f:
    code = f.read()

old = '''                        self.log("  ✓ CAPTCHA solved automatically!")
                        await asyncio.sleep(1)
                        return True'''

new = '''                        self.log("  ✓ CAPTCHA solved automatically!")
                        await asyncio.sleep(2)
                        await self.page.evaluate("""()=>{
                            const b=[...document.querySelectorAll('input[type=submit],button[type=submit],button')]
                                .find(x=>(x.value||x.textContent||'').toLowerCase().includes('submit'));
                            if(b){b.click();return;}
                            const f=document.querySelector('form');if(f)f.submit();
                        }""")
                        await asyncio.sleep(5)
                        return True'''

if old in code:
    code = code.replace(old, new)
    with open('discover.py','w') as f:
        f.write(code)
    print("Fixed - submit added after CAPTCHA solve")
else:
    print("Pattern not found")
