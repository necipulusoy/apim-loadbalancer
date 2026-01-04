# APIM Load Balancer

Bu çalışma, APIM üzerinden **load balancing + failover** senaryosunu test etmek için hazırlanmıştır.
Uygulama doğrudan Azure OpenAI’a gitmez; APIM üzerinden gider.

---

## 1) Ön Koşullar

- 3 adet Azure OpenAI deployment (aynı model/deployment adı ile)
- 1 adet APIM (backend pool oluşturulmuş)
- APIM Managed Identity için `Cognitive Services OpenAI User` rolü verilmiş olmalı

> Öneri: 3 OpenAI’da **deployment adı aynı** olsun: `gpt-4o-mini`

---

## 2) APIM Policy (Load Balancer)

APIM → API → Inbound Policy içine ekle:

```xml
<policies>
  <inbound>
    <base />
    <authentication-managed-identity resource="https://cognitiveservices.azure.com"
      output-token-variable-name="managed-id-access-token" ignore-error="false" />
    <set-header name="Authorization" exists-action="override">
      <value>@("Bearer " + (string)context.Variables["managed-id-access-token"])</value>
    </set-header>

    <!-- Weighted routing: 70/20/10 -->
    <set-variable name="rand" value="@(new Random().Next(0,100))" />
    <choose>
      <when condition="@((int)context.Variables["rand"] < 70)">
        <set-variable name="backend-id" value="<azure-openai-chat-openai>-endpoint" />
        <set-backend-service backend-id="openai-first-backend" />
      </when>
      <when condition="@((int)context.Variables["rand"] < 90)">
        <set-variable name="backend-id" value="openai-second-backend" />
        <set-backend-service backend-id="openai-second-backend" />
      </when>
      <otherwise>
        <set-variable name="backend-id" value="openai-third-backend" />
        <set-backend-service backend-id="openai-third-backend" />
      </otherwise>
    </choose>
  </inbound>

  <backend>
    <retry count="2" interval="0" first-fast-retry="true"
      condition="@(context.Response.StatusCode == 429 || context.Response.StatusCode == 503)">
      <forward-request buffer-request-body="true" />
    </retry>
  </backend>

  <outbound>
    <set-header name="x-openai-backend" exists-action="override">
      <value>@(context.Variables.GetValueOrDefault<string>("backend-id") ?? "unknown")</value>
    </set-header>
    <base />
  </outbound>

  <on-error>
    <base />
  </on-error>
</policies>

```

---

## 3) Namespace

Ayrı namespace kullanıyoruz:

```bash
kubectl create namespace apim-lb
```

---

## 4) Secret (APIM Subscription Key)

```bash
kubectl create secret generic apim-subscription-secret \
  --from-literal=subscription-key=YOUR_APIM_SUBSCRIPTION_KEY \
  -n apim-lb \
  --dry-run=client -o yaml | kubectl apply -f -
```

---

## 5) Backend Ayarları

`apim-load-balancer/aksyamls/backend.yml`

Örnek env:

```yaml
- name: AZURE_OPENAI_DEPLOYMENT
  value: "gpt-4o-mini"
- name: AZURE_OPENAI_API_VERSION
  value: "2024-12-01-preview"
- name: APIM_BASE_URL
  value: "https://api-management.azure-api.net"
- name: APIM_SUBSCRIPTION_KEY
  valueFrom:
    secretKeyRef:
      name: apim-subscription-secret
      key: subscription-key
```

---

## 6) Deploy

```bash
kubectl apply -f apim-load-balancer/aksyamls/redis.yml
kubectl apply -f apim-load-balancer/aksyamls/backend.yml
kubectl apply -f apim-load-balancer/aksyamls/frontend.yml
```

---

## 7) Port Forward

```bash
kubectl -n apim-lb port-forward svc/openaifrontend-svc 8082:80
```

Tarayıcı: `http://localhost:8082`

---

## 8) Test (Load Balancer)

1) UI’dan mesaj gönder
2) Mesaj altında **Backend: ...** görünmeli
3) Bir backend’e 429 zorla → diğer backend’e geçişi gör

---

## 9) Notlar

- `AZURE_OPENAI_ENDPOINT` ve `AZURE_OPENAI_API_KEY` **kullanılmıyor** (APIM üzerinden gidiyoruz)
- Deployment adı **tüm OpenAI’larda aynı** olmalı

