import urllib.request
with urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3) as response:
    assert response.status==200
