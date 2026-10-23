param([string]$JobFile)
$ErrorActionPreference = 'Stop'
# CAdESCOM uses UTF-16 strings by default. The server supplies Base64 of the exact
# UTF-8 bytes so the signature always covers the identical HTTP body.
try {
    if (-not $JobFile) { $JobFile = Read-Host 'Path to sign-....json' }
    $JobFile = $JobFile.Trim('"')
    $job = Get-Content -LiteralPath $JobFile -Raw -Encoding UTF8 | ConvertFrom-Json
    Write-Host ('Organization: ' + $job.organization + ' / ' + $job.inn)
    Write-Host ('Action: ' + $job.kind)
    Write-Host ('SHA-256: ' + $job.sha256)
    $bytes = [Convert]::FromBase64String($job.payload_base64)
    $sha = [Security.Cryptography.SHA256]::Create()
    $actualHash = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    $sha.Dispose()
    if ($actualHash -ne $job.sha256) { throw 'Payload checksum mismatch' }
    Write-Host ([Text.Encoding]::UTF8.GetString($bytes))
    $store = New-Object -ComObject CAdESCOM.Store
    $store.Open(2, 'My', 2)
    try {
        $certs = @()
        for ($i=1; $i -le $store.Certificates.Count; $i++) {
            $cert = $store.Certificates.Item($i)
            if ($cert.HasPrivateKey() -and $cert.ValidToDate -gt (Get-Date)) { $certs += $cert }
        }
        if ($certs.Count -eq 0) { throw 'No valid certificates with private keys. Connect the token and install CryptoPro CSP/CAdESCOM.' }
        for ($i=0; $i -lt $certs.Count; $i++) { Write-Host ('[{0}] {1} / {2}' -f ($i+1), $certs[$i].SubjectName, $certs[$i].Thumbprint) }
        $selected = [int](Read-Host 'Certificate number')
        if ($selected -lt 1 -or $selected -gt $certs.Count) { throw 'Invalid certificate number' }
        if ((Read-Host 'Sign this payload? Type YES') -cne 'YES') { throw 'Cancelled' }
        $signer = New-Object -ComObject CAdESCOM.CPSigner
        $signer.Certificate = $certs[$selected-1]
        $signer.CheckCertificate = $true
        $signed = New-Object -ComObject CAdESCOM.CadesSignedData
        $signed.ContentEncoding = 1
        $signed.Content = $job.payload_base64
        $signature = $signed.SignCades($signer, 1, [bool]$job.detached)
        $outputFile = $JobFile + '.sig'
        [IO.File]::WriteAllText($outputFile, ($signature -replace '\s',''), [Text.Encoding]::ASCII)
        Write-Host ('Saved: ' + $outputFile)
        Write-Host 'Upload the .sig file in the application signing dialog.'
    } finally { $store.Close() }
} catch { Write-Error $_; exit 1 }
