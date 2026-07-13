package main

import (
	"crypto/ed25519"
	"encoding/base64"
	"fmt"
	"os"
	"strings"
)

func main() {
	encoded := strings.TrimSpace(os.Getenv("MENHIR_STORE_SIGNING_KEY"))
	if encoded == "" {
		fmt.Fprintln(os.Stderr, "MENHIR_STORE_SIGNING_KEY is required")
		os.Exit(1)
	}

	key, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil || len(key) != ed25519.PrivateKeySize {
		fmt.Fprintln(os.Stderr, "MENHIR_STORE_SIGNING_KEY must be a base64 Ed25519 private key")
		os.Exit(1)
	}

	manifest, err := os.ReadFile("store.yaml")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	signature := ed25519.Sign(ed25519.PrivateKey(key), manifest)
	if err := os.WriteFile("store.yaml.sig", []byte(base64.StdEncoding.EncodeToString(signature)+"\n"), 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
