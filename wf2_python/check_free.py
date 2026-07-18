"""
Quick tool: run the FREE enrichers against any real domain to see them work — no DB, no sending.

  python check_free.py                 # defaults to a known Shopify store
  python check_free.py somecompany.com someone@somecompany.com
"""
import sys
import enrich_free

domain = sys.argv[1] if len(sys.argv) > 1 else "allbirds.com"
email = sys.argv[2] if len(sys.argv) > 2 else f"info@{domain}"

print(f"Domain: {domain}")
try:
    print(f"  tech detected : {enrich_free.detect_tech(domain)}")
except Exception as e:  # noqa: BLE001
    print(f"  tech detected : (failed) {e}")
print(f"  email check   : {email} -> {enrich_free.verify_email(email)}")
print(f"  dnspython     : {'available' if enrich_free._HAS_DNS else 'NOT installed'}")
