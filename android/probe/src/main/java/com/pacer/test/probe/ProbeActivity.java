package com.pacer.test.probe;

import android.app.Activity;
import android.content.ContentResolver;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.provider.ContactsContract;
import android.util.Log;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

public final class ProbeActivity extends Activity {
    private static final int PICK_CONTACT_REQUEST = 41;

    private TextView readback;
    private Button pickerButton;
    private boolean launchPickerOnResume;
    private boolean autoPickerLaunched;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        int padding = (int) (24 * getResources().getDisplayMetrics().density);
        root.setPadding(padding, padding, padding, padding);

        TextView instructions = new TextView(this);
        instructions.setText("Selection-only test. Choose exactly one synthetic contact.");
        root.addView(instructions, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        pickerButton = new Button(this);
        pickerButton.setText("Choose one contact");
        pickerButton.setOnClickListener(view -> launchContactPicker());
        root.addView(pickerButton, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        readback = new TextView(this);
        readback.setText("UNVERIFIED: no contact selected");
        readback.setContentDescription("selection_readback");
        readback.setImportantForAccessibility(TextView.IMPORTANT_FOR_ACCESSIBILITY_YES);
        readback.setTextIsSelectable(true);
        root.addView(readback, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        setContentView(root);
        launchPickerOnResume = getIntent().getBooleanExtra("pacer_test_launch_picker", false);
    }

    @Override
    protected void onPostResume() {
        super.onPostResume();
        if (launchPickerOnResume && !autoPickerLaunched) {
            autoPickerLaunched = true;
            launchPickerOnResume = false;
            pickerButton.performClick();
        }
    }

    private void launchContactPicker() {
        Intent picker = new Intent(
                Intent.ACTION_PICK,
                ContactsContract.CommonDataKinds.Phone.CONTENT_URI);
        showReadback("UNVERIFIED: awaiting new picker result");
        try {
            pickerButton.setEnabled(false);
            startActivityForResult(picker, PICK_CONTACT_REQUEST);
        } catch (RuntimeException error) {
            pickerButton.setEnabled(true);
            showReadback("UNVERIFIED: platform contact picker unavailable");
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != PICK_CONTACT_REQUEST) {
            return;
        }
        tracePickerResult(requestCode, resultCode, data);
        pickerButton.setEnabled(true);
        if (resultCode == RESULT_CANCELED) {
            showReadback("UNVERIFIED: picker returned RESULT_CANCELED");
            return;
        }
        if (resultCode != RESULT_OK) {
            showReadback("UNVERIFIED: picker returned non-OK resultCode=" + resultCode);
            return;
        }
        if (data == null || data.getData() == null) {
            showReadback("UNVERIFIED: picker returned RESULT_OK without a URI");
            return;
        }
        readSelectedPhoneRow(data.getData());
    }

    private void tracePickerResult(int requestCode, int resultCode, Intent data) {
        Uri uri = data == null ? null : data.getData();
        String uriPermission = "not-checkable";
        if (uri != null) {
            int permission = checkUriPermission(uri, android.os.Process.myPid(),
                    android.os.Process.myUid(), Intent.FLAG_GRANT_READ_URI_PERMISSION);
            uriPermission = permission == android.content.pm.PackageManager.PERMISSION_GRANTED
                    ? "granted" : "denied";
        }
        Log.i("PacerProbeResult", "RESULT_TRACE requestCode=" + requestCode
                + ";resultCode=" + resultCode
                + ";dataPresent=" + (data != null)
                + ";uriPresent=" + (uri != null)
                + ";scheme=" + (uri == null ? "none" : uri.getScheme())
                + ";authority=" + (uri == null ? "none" : uri.getAuthority())
                + ";readGrantFlag=" + (data != null
                        && (data.getFlags() & Intent.FLAG_GRANT_READ_URI_PERMISSION) != 0)
                + ";uriPermission=" + uriPermission);
    }

    private void readSelectedPhoneRow(Uri selectedUri) {
        String[] projection = {
                ContactsContract.CommonDataKinds.Phone._ID,
                ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME,
                ContactsContract.CommonDataKinds.Phone.NUMBER
        };
        try (Cursor cursor = getContentResolver().query(selectedUri, projection, null, null, null)) {
            if (cursor == null || !cursor.moveToFirst()) {
                showReadback("UNVERIFIED: selected row could not be read");
                return;
            }
            int idColumn = cursor.getColumnIndexOrThrow(
                    ContactsContract.CommonDataKinds.Phone._ID);
            int nameColumn = cursor.getColumnIndexOrThrow(
                    ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME);
            int numberColumn = cursor.getColumnIndexOrThrow(
                    ContactsContract.CommonDataKinds.Phone.NUMBER);
            String rowId = cursor.getString(idColumn);
            String displayName = cursor.getString(nameColumn);
            String number = cursor.getString(numberColumn);
            String key = normalizeUsNumber(number);
            if (rowId == null || displayName == null || key == null) {
                showReadback("UNVERIFIED: selected row lacks complete identity data");
                return;
            }
            showReadback("RESULT_OK: BOUND CANDIDATE\ncontact_row_id=" + rowId
                    + "\ndisplay_name=" + displayName
                    + "\nrecipient_key=" + key);
        } catch (SecurityException error) {
            Log.i("PacerProbeResult", "READBACK_TRACE outcome=security_exception;exception=SecurityException");
            showReadback("UNVERIFIED: picker did not grant readable selection evidence");
        } catch (IllegalArgumentException error) {
            Log.i("PacerProbeResult", "READBACK_TRACE outcome=invalid_uri;exception=IllegalArgumentException");
            showReadback("UNVERIFIED: selected URI is invalid");
        }
    }

    private static String normalizeUsNumber(String value) {
        if (value == null) {
            return null;
        }
        String digits = value.replaceAll("\\D", "");
        if (digits.length() == 11 && digits.startsWith("1")) {
            digits = digits.substring(1);
        }
        if (digits.length() != 10 || digits.charAt(0) == '0' || digits.charAt(0) == '1'
                || digits.charAt(3) == '0' || digits.charAt(3) == '1') {
            return null;
        }
        return "+1" + digits;
    }

    private void showReadback(String text) {
        readback.setText(text);
        readback.setContentDescription("selection_readback: " + text);
    }
}
