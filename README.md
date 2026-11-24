# Caisse vers FEC — CS1 / CS2

Application Streamlit pour convertir deux exports de caisse en fichiers FEC séparés.

- **CS1** : CSV de récapitulatif par jour et moyen de paiement. TVA calculée au taux paramétré (5,5 % par défaut).
- **CS2** : export `TransactionBasket.csv` UTF-16/tabulé. TVA lue dans `ReceiptText`, et comptes 707 et 44571 distincts par taux.
- Journaux, comptes, libellés et comptes d’encaissement sont modifiables dans l’interface.
- Tickets sans taux lu, à zéro, ou présentant des écarts sont signalés. Ils ne sont pas transformés silencieusement.

## Lancer

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Import

Les exports sont des fichiers texte FEC au séparateur `|`, avec dates `AAAAMMJJ`. Les deux téléchargements sont distincts : `ventes_CS1.fec` et `ventes_CS2.fec`.
