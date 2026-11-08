package com.pacer.test.seeder;

import android.Manifest;
import android.app.Activity;
import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.ContentValues;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Bundle;
import android.provider.ContactsContract;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

public final class SeederActivity extends Activity {
    private static final int WRITE_CONTACTS_REQUEST = 73;
    private static final String PREFS = "synthetic_fixture";
    private static final String RAW_CONTACT_ID = "raw_contact_id";
    private static final String FIXTURE_DISPLAY_NAME = "Pacer Probe Fixture 001";
    private static final String FIXTURE_RECIPIENT_KEY = "+12025550100";

    private TextView status;
    private boolean seedAfterPermission;
    private boolean clearAfterPermission;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int padding = (int) (24 * getResources().getDisplayMetrics().density);
        root.setPadding(padding, padding, padding, padding);

        TextView instructions = new TextView(this);
        instructions.setText("Temporary fixture tool. Creates only one synthetic contact.");
        root.addView(instructions, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        Button seed = new Button(this);
        seed.setText("Seed synthetic contact");
        seed.setOnClickListener(view -> requestWritePermission(true, false));
        root.addView(seed, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        Button clear = new Button(this);
        clear.setText("Clear synthetic fixture");
        clear.setOnClickListener(view -> requestWritePermission(false, true));
        root.addView(clear, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        status = new TextView(this);
        status.setText("READY: no fixture operation performed");
        status.setContentDescription("seeder_status");
        root.addView(status, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        setContentView(root);
    }

    private void requestWritePermission(boolean seed, boolean clear) {
        seedAfterPermission = seed;
        clearAfterPermission = clear;
        if (checkSelfPermission(Manifest.permission.WRITE_CONTACTS)
                == PackageManager.PERMISSION_GRANTED) {
            performPendingOperation();
            return;
        }
        requestPermissions(new String[]{Manifest.permission.WRITE_CONTACTS},
                WRITE_CONTACTS_REQUEST);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != WRITE_CONTACTS_REQUEST) {
            return;
        }
        if (grantResults.length == 0 || grantResults[0] != PackageManager.PERMISSION_GRANTED) {
            seedAfterPermission = false;
            clearAfterPermission = false;
            setStatus("BLOCKED: WRITE_CONTACTS permission was not granted");
            return;
        }
        performPendingOperation();
    }

    private void performPendingOperation() {
        boolean seed = seedAfterPermission;
        boolean clear = clearAfterPermission;
        seedAfterPermission = false;
        clearAfterPermission = false;
        if (clear) {
            clearFixture();
        } else if (seed) {
            seedFixture();
        }
    }

    private void seedFixture() {
        clearStoredFixture();
        Uri rawUri = null;
        try {
            ContentValues rawValues = new ContentValues();
            rawValues.putNull(ContactsContract.RawContacts.ACCOUNT_NAME);
            rawValues.putNull(ContactsContract.RawContacts.ACCOUNT_TYPE);
            rawUri = getContentResolver().insert(
                    ContactsContract.RawContacts.CONTENT_URI, rawValues);
            if (rawUri == null) {
                setStatus("FAILED: provider did not create a raw contact");
                return;
            }
            long rawId = ContentUris.parseId(rawUri);
            insertData(rawId, ContactsContract.CommonDataKinds.StructuredName.CONTENT_ITEM_TYPE,
                    ContactsContract.CommonDataKinds.StructuredName.DISPLAY_NAME,
                    FIXTURE_DISPLAY_NAME);
            insertPhone(rawId);
            verifyFixtureVisible();
            getPreferences(MODE_PRIVATE).edit().putLong(RAW_CONTACT_ID, rawId).apply();
            setStatus("SEEDED: one synthetic contact is ready");
        } catch (RuntimeException error) {
            if (rawUri != null) {
                getContentResolver().delete(rawUri, null, null);
            }
            setStatus("FAILED: fixture insertion was rolled back");
        }
    }

    private void insertData(long rawId, String mimeType, String valueColumn, String value) {
        ContentValues values = new ContentValues();
        values.put(ContactsContract.Data.RAW_CONTACT_ID, rawId);
        values.put(ContactsContract.Data.MIMETYPE, mimeType);
        values.put(valueColumn, value);
        if (ContactsContract.CommonDataKinds.Phone.CONTENT_ITEM_TYPE.equals(mimeType)) {
            values.put(ContactsContract.CommonDataKinds.Phone.TYPE,
                    ContactsContract.CommonDataKinds.Phone.TYPE_MOBILE);
        }
        Uri inserted = getContentResolver().insert(ContactsContract.Data.CONTENT_URI, values);
        if (inserted == null) {
            throw new IllegalStateException("Contacts provider rejected a fixture data row");
        }
    }

    private void insertPhone(long rawId) {
        ContentValues values = new ContentValues();
        values.put(ContactsContract.Data.RAW_CONTACT_ID, rawId);
        values.put(ContactsContract.Data.MIMETYPE,
                ContactsContract.CommonDataKinds.Phone.CONTENT_ITEM_TYPE);
        values.put(ContactsContract.CommonDataKinds.Phone.NUMBER, FIXTURE_RECIPIENT_KEY);
        values.put(ContactsContract.CommonDataKinds.Phone.TYPE,
                ContactsContract.CommonDataKinds.Phone.TYPE_MOBILE);
        Uri inserted = getContentResolver().insert(ContactsContract.Data.CONTENT_URI, values);
        if (inserted == null) {
            throw new IllegalStateException("Contacts provider rejected the synthetic phone row");
        }
    }

    private void verifyFixtureVisible() {
        ContentResolver resolver = getContentResolver();
        String[] projection = {
                ContactsContract.CommonDataKinds.Phone._ID,
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                ContactsContract.CommonDataKinds.Phone.NUMBER
        };
        String selection = ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME + "=? AND "
                + ContactsContract.CommonDataKinds.Phone.NUMBER + "=?";
        long deadline = android.os.SystemClock.elapsedRealtime() + 10000;
        while (android.os.SystemClock.elapsedRealtime() < deadline) {
            try (android.database.Cursor cursor = resolver.query(
                    ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                    projection, selection, new String[]{FIXTURE_DISPLAY_NAME, FIXTURE_RECIPIENT_KEY}, null)) {
                if (cursor != null && cursor.moveToFirst()) {
                    return;
                }
            }
            try {
                Thread.sleep(200);
            } catch (InterruptedException interrupted) {
                Thread.currentThread().interrupt();
                throw new IllegalStateException("interrupted while waiting for Contacts provider", interrupted);
            }
        }
        throw new IllegalStateException("Contacts provider did not expose the committed phone row");
    }

    private void clearFixture() {
        clearStoredFixture();
        setStatus("CLEARED: stored synthetic fixture removed");
    }

    private void clearStoredFixture() {
        long rawId = getPreferences(MODE_PRIVATE).getLong(RAW_CONTACT_ID, -1L);
        if (rawId >= 0) {
            Uri rawUri = ContentUris.withAppendedId(
                    ContactsContract.RawContacts.CONTENT_URI, rawId);
            getContentResolver().delete(rawUri, null, null);
        }
        getPreferences(MODE_PRIVATE).edit().remove(RAW_CONTACT_ID).apply();
    }

    private void setStatus(String value) {
        status.setText(value);
        status.setContentDescription("seeder_status: " + value);
    }
}
