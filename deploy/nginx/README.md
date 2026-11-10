# nginx para `tb.lexicode-consulting.pt` (antes e depois do certificado)

Feito **no servidor nomad, com sudo**. O vhost entra primeiro só em HTTP (porta 80); o certbot
usa-o para validar o domínio e acrescenta ele próprio o bloco HTTPS e o redirecionamento.
Não é preciso o container do relay estar a correr para emitir o certificado.

Pré-requisito: `tb.lexicode-consulting.pt  A  213.63.232.58` (sem AAAA) — já está.

## 1. Confirmar que nada colide

```bash
ls /etc/nginx/sites-enabled/
grep -rn "tb.lexicode-consulting.pt\|upstream tbrelay" /etc/nginx/ || echo "livre"
ss -ltnp | grep 10120 || echo "porta 10120 livre"
```

Os três têm de estar livres. Se a porta 10120 estiver ocupada, escolher outra e trocá-la no vhost
(`server 127.0.0.1:XXXX;`) e no `.env.nomad` (`RELAY_HOST_PORT=XXXX`).

## 2. Instalar o vhost (só HTTP)

```bash
sudo curl -fsSL -o /etc/nginx/sites-available/tbrelay \
  https://raw.githubusercontent.com/jorgealmeida1987/ir-turtlebots/main/deploy/nginx/tbrelay
sudo ln -s /etc/nginx/sites-available/tbrelay /etc/nginx/sites-enabled/tbrelay
sudo nginx -t && sudo systemctl reload nginx
```

`nginx -t` tem de dizer `syntax is ok` e `test is successful`. Se falhar, **não** fazer reload;
remover o link (`sudo rm /etc/nginx/sites-enabled/tbrelay`) e ver a mensagem de erro.

## 3. Verificar que o vhost responde

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://tb.lexicode-consulting.pt/healthz
```

- `502` → **certo**: o vhost do `tb` está ativo e o relay ainda não está a correr.
- `200` → o relay já está no ar.
- `404` → ainda é o site por omissão a responder; o vhost não ficou ativo (rever o passo 2).

## 4. Emitir o certificado

```bash
sudo certbot --nginx -d tb.lexicode-consulting.pt --redirect
```

O certbot valida o domínio pela porta 80, instala o certificado e reescreve
`/etc/nginx/sites-available/tbrelay` com o bloco `listen 443 ssl` e o redirecionamento 80 → 443.
A renovação fica automática (timer do certbot).

## 5. Verificar o HTTPS

```bash
echo | openssl s_client -connect tb.lexicode-consulting.pt:443 -servername tb.lexicode-consulting.pt 2>/dev/null \
  | openssl x509 -noout -subject -enddate
curl -s -o /dev/null -w "%{http_code}\n" https://tb.lexicode-consulting.pt/healthz
```

- `subject=CN = tb.lexicode-consulting.pt` → certificado certo (antes aparecia o do `aeroflow`).
- `502` até o container do relay estar a correr; depois `200`.

Assim que houver `200` em HTTPS, o robot1 (que está a tentar a cada 30 s) liga-se sozinho.

## Desfazer

```bash
sudo rm /etc/nginx/sites-enabled/tbrelay && sudo nginx -t && sudo systemctl reload nginx
```
