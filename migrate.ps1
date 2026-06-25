$files = Get-ChildItem -Recurse -Include *.ts,*.tsx,*.py,*.yml,*.yaml,*.json,*.html,*.css,*.md,*.conf,*.ini,*.example `
  | Where-Object { $_.FullName -notlike "*\.git*" -and $_.FullName -notlike "*node_modules*" -and $_.Name -ne "package-lock.json" }

$replacements = @(
  "13\.127\.94\.68\.sslip\.io",         "win365jackpot.com";
  "admin\.13\.127\.94\.68\.sslip\.io",  "admin.win365jackpot.com";
  "sa\.13\.127\.94\.68\.sslip\.io",     "sa.win365jackpot.com";
  "app\.13\.127\.94\.68\.sslip\.io",    "app.win365jackpot.com";
  "support\.13\.127\.94\.68\.sslip\.io","support.win365jackpot.com";
  "app\.clari5pay\.com",                "app.win365jackpot.com";
  "admin\.clari5pay\.com",              "admin.win365jackpot.com";
  "sa\.clari5pay\.com",                 "sa.win365jackpot.com";
  "support\.clari5pay\.com",            "support.win365jackpot.com";
  "clari5pay_token",                    "win365jackpot_token";
  "clari5pay_user",                     "win365jackpot_user";
  "clari5pay_support_token",            "win365jackpot_support_token";
  "clari5pay_support_user",             "win365jackpot_support_user";
  "container_name: clari5pay_",         "container_name: win365jackpot_";
  "clari5pay-frontend",                 "win365jackpot-frontend";
  "clari5pay-support",                  "win365jackpot-support";
  "clari5pay-audit-logs",               "win365jackpot-audit-logs";
  "clari5pay-complaints",               "win365jackpot-complaints";
  "clari5pay-report",                   "win365jackpot-report";
  "clari5pay-search",                   "win365jackpot-search";
  "@clari5pay\.io",                     "@win365jackpot.com";
  "no-reply@clari5pay\.io",             "no-reply@win365jackpot.com";
  "clari5pay\.c76auiocst4e",            "win365jackpot.c76auiocst4e";
  "clari5pay@hdfcbank",                 "win365jackpot@hdfcbank";
  "localhost:5432/clari5pay",           "localhost:5432/win365jackpot";
  "docker exec clari5pay_api",          "docker exec win365jackpot_api";
  "clari5pay\.geoip",                   "win365jackpot.geoip";
  "clari5pay\.email",                   "win365jackpot.email";
  "clari5pay-mumbai",                   "win365jackpot-mumbai";
  "clari5pay-app",                      "win365jackpot-app";
  "Clari5Pay",                          "Win365Jackpot";
  "clari5pay(?!.*gmail)",               "win365jackpot"
)

foreach ($file in $files) {
  try {
    $content = Get-Content $file.FullName -Raw -Encoding UTF8
    if ($null -eq $content) { continue }
    $original = $content
    for ($i = 0; $i -lt $replacements.Count; $i += 2) {
      $content = $content -replace $replacements[$i], $replacements[$i+1]
    }
    if ($content -ne $original) {
      [System.IO.File]::WriteAllText($file.FullName, $content, [System.Text.Encoding]::UTF8)
      Write-Host "Updated: $($file.Name)"
    }
  } catch {
    Write-Warning "Skipped $($file.Name): $_"
  }
}

Write-Host "`nAll done. Now run:"
Write-Host "  git add -A"
Write-Host "  git commit -m 'chore: migrate all domains from sslip.io/clari5pay.* to win365jackpot.com'"
Write-Host "  git push origin main"
