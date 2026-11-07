from zdaudit import keychain

# A test that forgets the fake keychain fails here instead of touching the real one.
keychain.SECURITY = "/nonexistent/security"
