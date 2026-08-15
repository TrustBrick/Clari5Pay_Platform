# Third-party assets

## Bank logos

`frontend/src/assets/banks/*.svg` — 49 bank logos (41 Indian, 8 international banks with Indian branches).

| | |
|---|---|
| Source | https://github.com/auraveni/global-bank-logos |
| Path in source | `assets/bank/indian-bank/` and `assets/bank/international-bank/` |
| Licence | MIT |
| Retrieved | 2026-08-15 |
| Used by | `frontend/src/utils/bankLogos.ts` (mapping) and `frontend/src/components/BankLogo.tsx` (render) |

Assets are vendored, not fetched at runtime: Vite bundles them at build time, so no request
leaves the browser for a third-party host when a logo renders.

### Trademark note

The MIT licence above covers the collection itself. It does **not**, and cannot, grant rights in
the bank trademarks the files depict — each logo remains the property of its respective bank.
They are used here to identify the bank a user has selected, which is nominative use, but this is
worth a look from whoever owns legal sign-off before this reaches production.

### MIT licence, as published by the source repository

```
MIT License

Copyright (c) 2026 Bank Logos Collection

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
