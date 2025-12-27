## X (Twitter) Lokal Bot Paneli

Basit bir Flask uygulaması ile `.env` içindeki birden fazla X hesabından birini seçip lokal web arayüzü üzerinden tweet atmanızı sağlar.

### 1. Kurulum / Setup

```bash
python -m venv venv
venv\Scripts\activate  # Windows
pip install -r requirements.txt
```

### 2. X Developer Portal'dan Bilgileri Alma / Getting Info from X Developer Portal

**EVET, bu bot resmi X API'sini kullanıyor!** (https://api.twitter.com/1.1/statuses/update.json)

**OAuth 1.0a kullanıyoruz** - Developer hesabı bilgileri ortak, her X hesabı için ayrı Access Token gerekir.

1. **X Developer Portal'a giriş yapın:**
   - https://developer.twitter.com/en/portal adresine gidin
   - X (Twitter) hesabınızla giriş yapın

2. **Bir App oluşturun veya mevcut App'i seçin:**
   - "Projects & Apps" bölümünden bir App seçin veya yeni bir App oluşturun
   - App'inizin adını not edin

3. **Developer Hesabı Bilgilerini Alın (ortak - tüm hesaplar için aynı):**
   - App'inizin detay sayfasına gidin
   - **"Keys and tokens"** sekmesine tıklayın
   - Şu 2 bilgiyi kopyalayın (Developer hesabı bilgileri):
     - **API Key** (Consumer Key)
     - **API Secret Key** (Consumer Secret)

4. **Uygulama İzinlerini Ayarlayın (ÖNEMLİ!):**
   - App'inizin ayarlarına gidin
   - **"Settings"** veya **"App permissions"** sekmesine tıklayın
   - İzin seviyesini **"Read and Write"** veya **"Read, Write, and Direct Messages"** olarak ayarlayın
   - Bu ayar olmadan tweet atamazsınız!

5. **OAuth 1.0a Ayarlarını Yapın:**
   - App'inizin **"User authentication settings"** sayfasına gidin
   - **"OAuth 1.0a"** seçeneğini etkinleştirin
   - **"Type of App"** olarak **"Web App, Automated App or Bot"** seçin
   - **"App permissions"** → **"Read and write"** veya üstü seçin
   - Ayarları kaydedin

6. **Her X Hesabı İçin Access Token ve Access Token Secret Alın:**
   
   **Yöntem 1: X Developer Portal'dan Direkt Alma (En Kolay)**
   
   - App'inizin **"Keys and tokens"** sekmesine gidin
   - **"Access Token & Access Token Secret"** bölümünü bulun
   - Eğer token yoksa, **"Generate"** veya **"Create"** butonuna tıklayın
   - Eğer token varsa ama başka bir hesap için kullanmak istiyorsanız, **"Regenerate"** butonuna tıklayın
   - **ÖNEMLİ:** Token oluştururken hangi X hesabıyla giriş yaptıysanız, o hesabın token'ı oluşturulur
   - Şu 2 bilgiyi kopyalayın:
     - **Access Token**
     - **Access Token Secret**
   
   **Yöntem 2: Birden Fazla Hesap İçin (Her Hesap İçin Ayrı Token):**
   
   - Her X hesabı için ayrı token almak için:
     1. X Developer Portal'dan çıkış yapın
     2. Token'ını almak istediğiniz X hesabıyla giriş yapın
     3. Aynı App'in "Keys and tokens" sayfasına gidin
     4. "Access Token & Access Token Secret" bölümünden token oluşturun
     5. Token'ları kopyalayın ve `.env` dosyasına ekleyin
     6. Bir sonraki hesap için tekrar çıkış yapıp o hesabın token'ını alın

7. **Birden fazla hesap için:**
   - Developer hesabı bilgileri (API Key, API Secret) ortak olacak
   - Her X hesabı için ayrı Access Token ve Access Token Secret gerekir
   - Tüm hesaplar aynı Developer App'i kullanabilir
   - Her hesabın token'ını almak için o hesabın X Developer Portal'a giriş yapması gerekir

### 3. .env Dosyası Oluşturma / Creating .env File

Proje kök dizinine (app.py ile aynı klasörde) bir `.env` dosyası oluşturun:

**Windows'ta:**
- Notepad veya herhangi bir metin editörü ile `.env` adında bir dosya oluşturun
- Dosya adı sadece `.env` olmalı (uzantı yok)

**Dosya içeriği örneği (OAuth 1.0a - Developer hesabı ortak):**

```env
APP_SECRET_KEY=degistir-bunu-rastgele-bir-string

# Developer Hesabı Bilgileri (ortak - tüm hesaplar için aynı)
X_API_KEY=AbCdEfGhIjKlMnOpQrStUvWxYz1234567
X_API_SECRET=abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ

# İlk X Hesabı / First X Account
X_ACCOUNT1_NAME=Ana Hesap
X_ACCOUNT1_ACCESS_TOKEN=1234567890-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ
X_ACCOUNT1_ACCESS_TOKEN_SECRET=abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ

# İkinci X Hesabı / Second X Account (isteğe bağlı / optional)
X_ACCOUNT2_NAME=Yan Hesap
X_ACCOUNT2_ACCESS_TOKEN=9876543210-ZYXWVUTSRQPONMLKJIHGFEDCBAzyxwvutsrqponmlkjihgfedcba
X_ACCOUNT2_ACCESS_TOKEN_SECRET=ZYXWVUTSRQPONMLKJIHGFEDCBA0987654321zyxwvutsrqponmlkjihgfedcba

# Üçüncü X Hesabı / Third X Account (isteğe bağlı / optional)
X_ACCOUNT3_NAME=Üçüncü Hesap
X_ACCOUNT3_ACCESS_TOKEN=5555555555-MnOpQrStUvWxYzAbCdEfGhIjKl1234567890
X_ACCOUNT3_ACCESS_TOKEN_SECRET=MnOpQrStUvWxYzAbCdEfGhIjKl1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ
```

**Açıklama / Explanation:**
- `APP_SECRET_KEY`: Flask için rastgele bir string (güvenlik için)
- `X_API_KEY`: Developer hesabı **API Key** (ortak - tüm hesaplar için aynı)
- `X_API_SECRET`: Developer hesabı **API Secret** (ortak - tüm hesaplar için aynı)
- `X_ACCOUNT1_NAME`: Web arayüzünde görünecek hesap adı (istediğiniz gibi değiştirebilirsiniz)
- `X_ACCOUNT1_ACCESS_TOKEN`: Her X hesabı için ayrı **Access Token**
- `X_ACCOUNT1_ACCESS_TOKEN_SECRET`: Her X hesabı için ayrı **Access Token Secret**
- İkinci, üçüncü hesap eklemek için `X_ACCOUNT2_*`, `X_ACCOUNT3_*` şeklinde devam edin
- **Developer hesabı bilgileri (X_API_KEY, X_API_SECRET) ortak olacak**
- **Her X hesabı için 3 bilgi zorunludur**: `NAME`, `ACCESS_TOKEN`, `ACCESS_TOKEN_SECRET`

**Örnek .env dosyası (2 hesap ile):**

```env
APP_SECRET_KEY=my-super-secret-key-12345

# Developer Hesabı (ortak)
X_API_KEY=AbCdEfGhIjKlMnOpQrStUvWxYz1234567
X_API_SECRET=abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ

# İlk X Hesabı
X_ACCOUNT1_NAME=Kişisel Hesabım
X_ACCOUNT1_ACCESS_TOKEN=1234567890-abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ
X_ACCOUNT1_ACCESS_TOKEN_SECRET=abcdefghijklmnopqrstuvwxyz1234567890ABCDEFGHIJKLMNOPQRSTUVWXYZ

# İkinci X Hesabı
X_ACCOUNT2_NAME=İş Hesabım
X_ACCOUNT2_ACCESS_TOKEN=9876543210-ZYXWVUTSRQPONMLKJIHGFEDCBAzyxwvutsrqponmlkjihgfedcba
X_ACCOUNT2_ACCESS_TOKEN_SECRET=ZYXWVUTSRQPONMLKJIHGFEDCBA0987654321zyxwvutsrqponmlkjihgfedcba
```

### 3. Çalıştırma / Run

```bash
python app.py
```

Tarayıcıdan `http://127.0.0.1:5000` adresine gidin. Uygulama sadece lokal makinenizde çalışır.


