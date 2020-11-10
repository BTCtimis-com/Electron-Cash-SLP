#!/usr/bin/env python3
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2015 Thomas Voegtlin
#
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction,
# including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
# BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from electroncash.i18n import _, ngettext
import electroncash.web as web
import electroncash.cashscript as cashscript
from electroncash.address import Address, PublicKey, hash160
from electroncash.bitcoin import TYPE_ADDRESS, push_script
from electroncash.contacts import Contact, ScriptContact, contact_types
from electroncash.plugins import run_hook
from electroncash.transaction import Transaction
from electroncash.util import FileImportFailed, PrintError, finalization_print_error
from electroncash.slp import SlpNoMintingBatonFound, buildMintOpReturnOutput_V1
# TODO: whittle down these * imports to what we actually use when done with
# our changes to this class -Calin
from PyQt5.QtGui import *
from PyQt5.QtCore import *
from PyQt5.QtWidgets import *
from .util import (MyTreeWidget, webopen, WindowModalDialog, Buttons,
                   CancelButton, OkButton, HelpLabel, WWLabel,
                   destroyed_print_error, webopen, ColorScheme, MONOSPACE_FONT,
                   rate_limited)
from enum import IntEnum
from collections import defaultdict
from typing import List, Set, Dict, Tuple
import itertools

from .slp_create_token_mint_dialog import SlpCreateTokenMintDialog

class ContactList(PrintError, MyTreeWidget):
    filter_columns = [1, 2, 3]  # Name, Label, Address
    default_sort = MyTreeWidget.SortSpec(1, Qt.AscendingOrder)

    do_update_signal = pyqtSignal()
    unspent_coins_dl_signal = pyqtSignal(dict, bool)

    class DataRoles(IntEnum):
        Contact     = Qt.UserRole + 0

    def __init__(self, parent):
        MyTreeWidget.__init__(self, parent, self.create_menu,
                              ["", _('Name'), _('Label'), _('Address'), _('Type'), _('Script Coins') ], 2, [1,2],  # headers, stretch_column, editable_columns
                              deferred_updates=True, save_sort_settings=True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setSortingEnabled(True)
        self.wallet = parent.wallet
        self.main_window = parent
        self.setIndentation(0)
        self._edited_item_cur_sel = (None,) * 3
        self.monospace_font = QFont(MONOSPACE_FONT)
        self.cleaned_up = False
        self.do_update_signal.connect(self.update)
        self.icon_contacts = QIcon(":icons/tab_contacts.png")
        self.icon_unverif = QIcon(":icons/unconfirmed.svg")

        self.unspent_coins_dl_signal.connect(self.got_unspent_coins_response_slot)
        self.addr_txos = {}

        # fetch unspent script coins
        script_addrs = [c.address for c in self.parent.contacts.data if c.type == 'script' ]
        self.fetch_script_coins(script_addrs, display_error=False)

    def clean_up(self):
        self.cleaned_up = True
        # except TypeError: pass
        try: self.do_update_signal.disconnect(self.update)
        except TypeError: pass
        try: self.parent.gui_object.cashaddr_toggled_signal.disconnect(self.update)
        except TypeError: pass

    def on_permit_edit(self, item, column):
        # openalias items shouldn't be editable
        if column == 2: # Label, always editable
            return True
        return item.data(0, self.DataRoles.Contact).type in ('address', 'script')

    def on_edited(self, item, column, prior_value):
        contact = item.data(0, self.DataRoles.Contact)
        if column == 2: # Label
            label_key = contact.address
            try: label_key = Address.from_string(label_key).to_storage_string()
            except: pass
            self.wallet.set_label(label_key, item.text(2))
            self.update() # force refresh in case 2 contacts use the same address
            return
        # else.. Name
        typ = contact.type
        was_cur, was_sel = bool(self.currentItem()), item.isSelected()
        name, value = item.text(1), item.text(3)
        del item  # paranoia

        # On success, parent.set_contact returns the new key (address text)
        # if 'cashacct'.. or always the same key for all other types.
        key = self.parent.set_contact(name, value, typ=typ, replace=contact)

        if key:
            # Due to deferred updates, on_update will actually be called later.
            # So, we have to save the edited item's "current" and "selected"
            # status here. 'on_update' will look at this tuple and clear it
            # after updating.
            self._edited_item_cur_sel = (key, was_cur, was_sel)

    def import_contacts(self):
        wallet_folder = self.parent.get_wallet_folder()
        filename, __ = QFileDialog.getOpenFileName(self.parent, "Select your wallet file", wallet_folder)
        if not filename:
            return
        try:
            num = self.parent.contacts.import_file(filename)
            self.parent.show_message(_("{} contacts successfully imported.").format(num))
        except Exception as e:
            self.parent.show_error(_("Electron Cash was unable to import your contacts.") + "\n" + repr(e))
        self.on_update()

    def export_contacts(self):
        if self.parent.contacts.empty:
            self.parent.show_error(_("Your contact list is empty."))
            return
        try:
            fileName = self.parent.getSaveFileName(_("Select file to save your contacts"), 'electron-cash-contacts.json', "*.json")
            if fileName:
                num = self.parent.contacts.export_file(fileName)
                self.parent.show_message(_("{} contacts exported to '{}'").format(num, fileName))
        except Exception as e:
            self.parent.show_error(_("Electron Cash was unable to export your contacts.") + "\n" + repr(e))

    def find_item(self, key: Contact) -> QTreeWidgetItem:
        ''' Rather than store the item reference in a lambda, we store its key.
        Storing the item reference can lead to C++ Runtime Errors if the
        underlying QTreeWidgetItem is deleted on .update() while the right-click
        menu is still up. This function returns a currently alive item given a
        key. '''
        for item in self.get_leaves():
            if item.data(0, self.DataRoles.Contact) == key:
                return item

    def _on_edit_item(self, key : Contact, column : int):
        ''' Callback from context menu, private method. '''
        item = self.find_item(key)
        if item:
            self.editItem(item, column)

    @staticmethod
    def _i2c(item : QTreeWidgetItem) -> Contact:
        return item.data(0, ContactList.DataRoles.Contact)

    def create_menu(self, position):
        menu = QMenu()
        selected = self.selectedItems()
        i2c = self._i2c
        if selected:
            names = [item.text(1) for item in selected]
            keys = [i2c(item) for item in selected]
            payable_keys = [k for k in keys if k.type != 'script']
            deletable_keys = [k for k in keys if k.type in contact_types]
            column = self.currentColumn()
            column_title = self.headerItem().text(column)
            column_data = '\n'.join([item.text(column) for item in selected])
            item = self.currentItem()
            typ = i2c(item).type if item else 'unknown'
            if len(selected) > 1:
                column_title += f" ({len(selected)})"
            if len(selected) == 1:
                sel = i2c(selected[0])
                if sel.type == 'script':
                    menu.addAction("Check For Coins", lambda: self.fetch_script_coins([sel.address]))
                    addr = Address.from_string(sel.address)
                    if len(self.addr_txos.get(addr, [])) > 0:
                        if sel.sha256 == cashscript.SLP_VAULT_ID:
                            if cashscript.is_mine(self.wallet, sel.address)[0]:
                                menu.addAction(_("Sweep"), lambda: self.slp_vault_sweep(sel))
                            inputs = [ self.wallet.transactions.get(coin['tx_hash']).inputs() for coin in self.addr_txos.get(addr, []) if self.wallet.transactions.get(coin['tx_hash']) ]
                            can_revoke = False
                            for _in in itertools.chain(*inputs):
                                if self.wallet.is_mine(_in['address']):
                                    can_revoke = True
                                    break
                            if can_revoke:
                                menu.addAction(_("Revoke"), lambda: self.slp_vault_revoke(sel))
                        elif sel.sha256 == cashscript.SLP_MINT_GUARD_ID:
                            if cashscript.is_mine(self.wallet, sel.address)[0]:
                                token_id = sel.params['tokenId']
                                try:
                                    baton = self.wallet.get_slp_token_baton(token_id)
                                    for txo in self.addr_txos.get(addr):
                                        if baton['prevout_hash'] == txo['tx_hash'] and baton['prevout_n'] == txo['tx_pos']:
                                            menu.addAction(_("Mint Tool..."), lambda: SlpCreateTokenMintDialog(self.parent, token_id)) # lambda: self.slp_mint_guard_mint(baton))
                                            menu.addAction(_("Create New Contact for Mint Baton Transfer..."), lambda: self.create_slp_mint_guard_pin(token_id))
                                            transfer_candidates = self.get_related_mint_guards(sel)
                                            action = menu.addAction(_("Transfer Mint Baton..."), lambda: self.transfer_slp_mint_guard(baton, sel, transfer_candidates))
                                            if len(transfer_candidates) == 0:
                                                action.setDisabled(True)
                                            break
                                except SlpNoMintingBatonFound:
                                    pass
                    menu.addSeparator()
            menu.addAction(_("Copy {}").format(column_title), lambda: self.parent.app.clipboard().setText(column_data))
            if item and column in self.editable_columns and self.on_permit_edit(item, column):
                key = item.data(0, self.DataRoles.Contact)
                # this key & find_item business is so we don't hold a reference
                # to the ephemeral item, which may be deleted while the
                # context menu is up.  Accessing the item after on_update runs
                # means the item is deleted and you get a C++ object deleted
                # runtime error.
                menu.addAction(_("Edit {}").format(column_title), lambda: self._on_edit_item(key, column))
            a = menu.addAction(_("Pay to"), lambda: self.parent.payto_contacts(payable_keys))
            a = menu.addAction(_("Delete"), lambda: self.parent.delete_contacts(deletable_keys))
            if not deletable_keys:
                a.setDisabled(True)
            # Add sign/verify and encrypt/decrypt menu - but only if just 1 thing selected
            if len(keys) == 1 and Address.is_valid(keys[0].address):
                signAddr = Address.from_string(keys[0].address)
                a = menu.addAction(_("Sign/verify message") + "...", lambda: self.parent.sign_verify_message(signAddr))
                if signAddr.kind != Address.ADDR_P2PKH:
                    a.setDisabled(True)  # We only allow this for P2PKH since it makes no sense for P2SH (ambiguous public key)
            URLs = [web.BE_URL(self.config, 'addr', Address.from_string(key.address))
                    for key in keys if Address.is_valid(key.address)]
            a = menu.addAction(_("View on block explorer"), lambda: [URL and webopen(URL) for URL in URLs])
            if not any(URLs):
                a.setDisabled(True)
            menu.addSeparator()

        menu.addAction(self.icon_contacts, _("Add Contact") + " - " + _("Address"), self.parent.new_contact_dialog)
        #menu.addAction(self.icon_contacts, _("Add Issuer Contract"), self.connect_to_issuer)
        run_hook('create_contact_menu', menu, selected)
        menu.exec_(self.viewport().mapToGlobal(position))

    def slp_vault_sweep(self, item):
        coins = self.addr_txos.get(Address.from_string(item.address), [])
        for coin in coins:
            coin['prevout_hash'] = coin['tx_hash']
            coin['prevout_n'] = coin['tx_pos']
            coin['slp_vault_pkh'] = item.params['pkh']
            coin['address'] = Address.from_string(item.address)
        self.parent.sweep_slp_vault(coins)

    def slp_vault_revoke(self, item):
        coins = self.addr_txos.get(Address.from_string(item.address), [])
        for coin in coins:
            coin['prevout_hash'] = coin['tx_hash']
            coin['prevout_n'] = coin['tx_pos']
            coin['slp_vault_pkh'] = item.params['pkh']
            coin['address'] = Address.from_string(item.address)
        self.parent.revoke_slp_vault(coins)

    def create_slp_mint_guard_pin(self, token_id):
        pubkey = None
        dlg = QInputDialog(self)
        dlg.setInputMode(QInputDialog.TextInput)
        dlg.setLabelText('Public Key Hex:')
        dlg.setWindowTitle('Enter the Public Key of the new mint baton owner')
        dlg.resize(600,100)
        ok = dlg.exec_()
        text = dlg.textValue()
        if not ok:
            return
        try:
            PublicKey.from_string(text)
            pubkey = bytes.fromhex(text)
        except Exception as e:
            self.main_window.show_message(str(e))
            return

        # TODO: check for an already existing pin for this pubkey

        user_addr = Address.from_pubkey(pubkey)
        script_params = {
            'scriptBaseSha256': cashscript.SLP_MINT_GUARD_ID,
            'slpMintFront': cashscript.SLP_MINT_FRONT,
            'tokenId': token_id,
            'pkh': user_addr.hash160.hex()
        }
        pin_op_return_msg = cashscript.build_script_pin_output(cashscript.SLP_MINT_GUARD_ID, script_params)
        outputs = [pin_op_return_msg, (TYPE_ADDRESS, user_addr, 546), (TYPE_ADDRESS, self.wallet.get_unused_address(), 546)]
        tx = self.wallet.make_unsigned_transaction(self.main_window.get_coins(), outputs, self.main_window.config)
        self.main_window.show_transaction(tx, "New script pin for: %s"%cashscript.SLP_MINT_GUARD_NAME)  # TODO: can we have a callback after successful broadcast?

    def transfer_slp_mint_guard(self, baton, current_owner, candidates):
        pubkey_str = None
        pkh_str = None
        dlg = QInputDialog(self)
        dlg.setInputMode(QInputDialog.TextInput)
        dlg.setLabelText('Public Key Hex:')
        dlg.setWindowTitle('Enter the Public Key of the new mint baton owner')
        dlg.resize(600,100)
        ok = dlg.exec_()
        pubkey_str = dlg.textValue()
        if not ok:
            return
        try:
            PublicKey.from_string(pubkey_str)
            pkh_str = hash160(bytes.fromhex(pubkey_str)).hex()
        except Exception as e:
            self.main_window.show_message(str(e))
            return

        # try to find the pinned contract associated with this pubkey
        contact = [c for c in self.parent.contacts.data if c.sha256 == cashscript.SLP_MINT_GUARD_ID and c.params[3] == pkh_str][0]

        # add info to baton for Mint Guard Transfer signing
        baton['type'] = cashscript.SLP_MINT_GUARD_TRANSFER
        baton['slp_mint_guard_transfer_pk'] = pubkey_str
        owner_p2pkh = cashscript.get_p2pkh_owner_address(cashscript.SLP_MINT_GUARD_ID, contact.params)
        baton['slp_mint_guard_pkh'] = current_owner.params[3]
        token_id_hex = contact.params[2]
        baton['slp_token_id'] = token_id_hex
        baton['slp_mint_amt'] = int(0).to_bytes(8, 'big').hex()
        token_rec_script = self.wallet.get_unused_address().to_script_hex()
        baton['token_receiver_out'] = int(546).to_bytes(8, 'little').hex() + push_script(token_rec_script)
        current_baton_owner_addr = cashscript.get_p2pkh_owner_address(cashscript.SLP_MINT_GUARD_ID, current_owner.params)
        self.wallet.add_input_sig_info(baton, current_baton_owner_addr)

        # set outputs
        outputs = []
        slp_op_return_msg = buildMintOpReturnOutput_V1(token_id_hex, 2, 0, 'SLP1')
        outputs.append(slp_op_return_msg)
        outputs.append((TYPE_ADDRESS, self.wallet.get_unused_address(), 546))
        new_baton_address = Address.from_string(contact.address)
        outputs.append((TYPE_ADDRESS, new_baton_address, 546))

        tx = self.main_window.wallet.make_unsigned_transaction(self.main_window.get_coins(), outputs, self.main_window.config, mandatory_coins=[baton])
        self.main_window.show_transaction(tx, "Mint guard transfer")

    def get_related_mint_guards(self, contact):
        token_id = contact.params['tokenId']
        transfer_candidates = [ c for c in self.parent.contacts.data if isinstance(c, ScriptContact) and c.sha256 == cashscript.SLP_MINT_GUARD_ID and c.params['tokenId'] == token_id and c is not contact ]
        return transfer_candidates

    def fetch_script_coins(self, addresses, *, display_error=True):
        for addr in addresses:
            cashaddr = Address.from_string(addr).to_full_string(Address.FMT_CASHADDR)
            def callback(response):
                self.unspent_coins_dl_signal.emit(response, display_error)
            requests = [ ('blockchain.address.listunspent', [cashaddr]) ]
            self.parent.network.send(requests, callback)

    @pyqtSlot(dict, bool)
    def got_unspent_coins_response_slot(self, response, display_error):
        if response.get('error'):
            if not display_error:
                return
            self.main_window.show_error("The server you're connected to cannot be used fetch unspent coins.\n\nConnect to a different server and try again.")
            return
        raw = response.get('result')
        self.addr_txos[Address.from_string(response.get('params')[0])] = raw
        self.update()

    def get_full_contacts(self, include_pseudo: bool = True) -> List[Contact]:
        ''' Returns all the contacts, with the "My CashAcct" pseudo-contacts
        clobbering dupes of the same type that were manually added.
        Client code should scan for type == 'cashacct' and type == 'cashacct_W' '''
        return self.parent.contacts.get_all(nocopy=True)

    @rate_limited(0.333, ts_after=True) # We rate limit the contact list refresh no more 3 per second
    def update(self):
        if self.cleaned_up:
            # short-cut return if window was closed and wallet is stopped
            return
        super().update()

    def on_update(self):
        if self.cleaned_up:
            return
        item = self.currentItem()
        current_contact = item.data(0, self.DataRoles.Contact) if item else None
        selected = self.selectedItems() or []
        selected_contacts = set(item.data(0, self.DataRoles.Contact) for item in selected)
        del item, selected  # must not hold a reference to a C++ object that will soon be deleted in self.clear()..
        self.clear()
        type_names = defaultdict(lambda: _("Unknown"))
        type_names.update({
            # 'openalias'  : _('OpenAlias'),
            'script'     : _('Script'),
            'address'    : _('Address'),
        })
        type_icons = {
            # 'openalias'  : self.icon_openalias,
            'script'     : self.icon_contacts,
            'address'    : self.icon_contacts,
        }
        selected_items, current_item = [], None
        edited = self._edited_item_cur_sel
        for contact in self.get_full_contacts():
            _type, name, address = contact.type, contact.name, contact.address
            label_key = address
            if _type in ('address'):
                try:
                    # try and re-parse and re-display the address based on current UI string settings
                    addy = Address.from_string(address)
                    address = addy.to_ui_string()
                    label_key = addy.to_storage_string()
                    del addy
                except:
                    ''' This may happen because we may not have always enforced this as strictly as we could have in legacy code. Just move on.. '''
            label = self.wallet.get_label(label_key)
            item = QTreeWidgetItem(["", name, label, address, type_names[_type]])
            item.setData(0, self.DataRoles.Contact, contact)
            item.DataRole = self.DataRoles.Contact
            if _type in type_icons:
                item.setIcon(4, type_icons[_type])
            # always give the "Address" field a monospace font even if it's
            # not strictly an address such as openalias...
            item.setFont(3, self.monospace_font)
            self.addTopLevelItem(item)
            if contact == current_contact or (contact == edited[0] and edited[1]):
                current_item = item  # this key was the current item before and it hasn't gone away
            if contact in selected_contacts or (contact == edited[0] and edited[2]):
                selected_items.append(item)  # this key was selected before and it hasn't gone away

            # show script Utxos count
            if _type == 'script':
                cashaddr = Address.from_string(address)
                if cashscript.is_mine(self.wallet, address)[0] and cashaddr not in self.wallet.contacts_subscribed:
                    self.wallet.contacts_subscribed.append(cashaddr)
                    self.wallet.synchronizer.subscribe_to_addresses([cashaddr])
                txos = self.addr_txos.get(cashaddr, [])
                if len(txos) > 0:
                    item.setText(5, str(len(txos)))

        if selected_items:  # sometimes currentItem is set even if nothing actually selected. grr..
            # restore current item & selections
            if current_item:
                # set the current item. this may also implicitly select it
                self.setCurrentItem(current_item)
            for item in selected_items:
                # restore the previous selection
                item.setSelected(True)
        self._edited_item_cur_sel = (None,) * 3
        run_hook('update_contacts_tab', self)
