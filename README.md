# Job Radar

Revisa 3 veces al día los portales de empleo **de las propias empresas** (Workday, Greenhouse, Lever, SmartRecruiters, Oracle, etc.) y te avisa por **Telegram** cuando aparece una vacante nueva que pasa tus filtros.

- **Vos elegís** las empresas, el país o las ciudades, y si querés vacantes **remotas para LATAM**.
- Corre gratis en GitHub Actions: no necesitás dejar la compu encendida.
- **Costo: ₡0 / $0.** Un repo privado tiene 2.000 minutos gratis al mes y cada corrida usa ~5. Si se acabaran, GitHub detiene las corridas; no cobra nada mientras tu cuenta no tenga tarjeta registrada.

---

## Configuración (10 minutos, todo desde el navegador)

### 1. Crear tu copia
Arriba en esta página: **Use this template → Create a new repository**. Ponele un nombre y marcá **Private**. Es importante: el radar guarda en tu repo las vacantes que ya te avisó.

### 2. Crear el bot de Telegram
1. En Telegram buscá **@BotFather** y mandale `/newbot`.
2. Elegí un nombre y un usuario que termine en `bot`.
3. Copiá el **token** que te da (algo como `123456789:AAH...`).

### 3. Obtener tu chat ID
1. Abrí el chat con tu bot nuevo y tocá **Iniciar** (o mandale `/start`).
2. En el navegador abrí esta dirección, con tu token en lugar de `TOKEN`:
   `https://api.telegram.org/botTOKEN/getUpdates`
3. Buscá `"chat":{"id":` y copiá el número que sigue (por ejemplo `987654321`).
   Si sale `"result":[]`, mandale otro mensaje al bot y recargá la página.

### 4. Guardar los dos datos como Secrets
En **tu** repo: **Settings → Secrets and variables → Actions → New repository secret**. Creá dos:

| Name | Value |
|---|---|
| `TELEGRAM_TOKEN` | el token del paso 2 |
| `TELEGRAM_CHAT_ID` | el número del paso 3 |

### 5. Decirle qué buscar
**Actions → Configurar radar → Run workflow** y llená el formulario:

| Campo | Qué poner | Ejemplo |
|---|---|---|
| **Empresas** | Nombres o links, separados por coma | `Gorilla Logic, BairesDev, Globant` |
| **Ubicaciones** | Países o ciudades donde podés ir presencial o híbrido. Vacío si solo querés remoto | `Venezuela, Caracas` |
| **Remoto** | `latam` = "Remote LATAM / Latin America / Americas" · `global` = además "Worldwide / Anywhere" · `no` = solo tus ubicaciones | `latam` |
| **Palabras** | Lo que debe decir el título del puesto. Vacío = usar las de datos y análisis que ya trae | `data, analyst, sql, power bi` |

Tocá **Run workflow**. En 1–2 minutos te llega a Telegram un resumen con cada empresa:
- ✓ si encontró su portal y cuántas vacantes pasan tus filtros.
- ✗ si no la encontró. Para esas, abrí una vacante de la empresa, tocá **Apply** y pegá ese link en el formulario la próxima vez.

Apenas termina, el radar corre solo y te manda las vacantes abiertas. Desde ahí te avisa solo de las nuevas, 3 veces al día (en Venezuela: 9:43, 14:43 y 19:43).

Podés volver a usar **Configurar radar** cuando quieras para agregar más empresas o cambiar la ubicación. Lo que dejes vacío no cambia.

---

## Ajustes a mano (opcional)

Todo se edita desde GitHub con el lápiz ✏️ del archivo y **Commit changes**.

### `perfil.txt` — dónde y qué buscar
Las mismas cuatro cosas del formulario, en palabras simples (mayúsculas y tildes no importan):
- `ubicaciones`: países o ciudades. Poné siempre el país.
- `remoto`: `latam`, `global` o `no`. Ojo: "Remote" a secas casi siempre significa "remoto dentro de EE. UU.", por eso no cuenta.
- `incluir`: palabras que debe tener el título. Con `*` al final vale cualquier terminación (`insight*` → insights). `bi` no encuentra "Bilingual".
- `excluir`: palabras que descartan el puesto (prácticas, call center, director...). Si te interesan puestos de dirección, quitá `director`.

### `companies.txt` — la lista de empresas
El formulario la llena solo. Si querés agregar una a mano: `Nombre | URL`. Para **silenciar** una empresa, poné `#` al inicio de su línea.

| Si el link de *Apply* tiene… | Línea |
|---|---|
| `myworkdayjobs.com` | `Empresa \| https://empresa.wd5.myworkdayjobs.com/SitioExterno` (sin la parte `/job/...`) |
| `greenhouse.io` | `Empresa \| https://job-boards.greenhouse.io/empresa` |
| `lever.co` | `Empresa \| https://jobs.lever.co/empresa` |
| `smartrecruiters.com` | `Empresa \| https://jobs.smartrecruiters.com/Empresa` |
| `ashbyhq.com` | `Empresa \| https://jobs.ashbyhq.com/empresa` |
| `apply.workable.com` | `Empresa \| https://apply.workable.com/empresa` |

Si el portal es otro (iCIMS, Taleo, Avature...), el radar no lo soporta y lo vas a ver como error.

### Horario — `.github/workflows/radar.yml`
La línea `cron: "43 13,18,23 * * *"` está en hora UTC. Por ejemplo, `"13 12 * * *"` = una vez al día a las 8:13 a. m. en Venezuela (UTC − 4).

---

## Si algo sale mal

- **El mensaje termina con "N fuentes con error":** en **Actions**, abrí la corrida y el paso *Correr radar*. Las líneas con ✗ dicen qué empresa falló. Si se repite, silenciala en `companies.txt`. Un error aislado suele ser una caída temporal del portal.
- **No me llega nada:** revisá en `perfil.txt` que haya ubicaciones o `remoto = latam`/`global`. Para ver todo lo que publica una empresa sin filtros, corré localmente `python job_radar.py --no-title-filter --only "Empresa"` (ver abajo).
- **Quiero que me reenvíe todo:** borrá `radar.db` del repo (**⋯ → Delete file**) cuando no haya una corrida activa, y corré **Job Radar → Run workflow**.
- **Recuperar la lista sin reenviar avisos:** cada corrida deja un CSV durante 5 días en **Actions → (la corrida) → Artifacts**.
- **"Faltan los Secrets":** revisá el paso 4. Los nombres tienen que ser exactamente `TELEGRAM_TOKEN` y `TELEGRAM_CHAT_ID`.

## Correrlo en tu compu (opcional)
```
pip install requests
python job_radar.py --telegram-setup                        # conecta el bot (una vez)
python job_radar.py                                         # una corrida
python job_radar.py --no-title-filter --only "Globant"      # ver todo lo de una empresa
EMPRESAS="Gorilla Logic, Globant" python configurar.py      # agregar empresas desde la terminal
```
