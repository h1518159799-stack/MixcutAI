# MixcutAI License Backup

Encrypted authorization-system backup:

private_backup/MixcutAI_License_Backup_20260914.7z

The archive uses AES-256 encryption with encrypted filenames.

The password is intentionally NOT stored in this repository.

Restore outline:

1. Clone the MixcutAI private GitHub repository.
2. Checkout backup/v1.3.27-handoff-20260914.
3. Install 7-Zip.
4. Decrypt private_backup/MixcutAI_License_Backup_20260914.7z using the separately stored password.
5. Restore the license recovery keys and migration materials.
6. Restore the administrator signing private key where required.
7. Restore license server/database/domain configuration using the files contained in the encrypted backup.
8. Verify license issuing, activation, unbind and migration operations.

Never commit decrypted private keys, databases, tokens or passwords to Git.
