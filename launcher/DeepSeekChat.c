/* Lanzador nativo de DeepSeek Chat portable: hace lo mismo que DeepSeekChat.bat
 *     start "" "<carpeta>\runtime\pythonw.exe" -I "<carpeta>\app\DeepSeekChat.pyw"
 * pero es un .exe con el ícono (asterisc.ico) embebido y sin ventana de consola que parpadee.
 *
 * Nativo y con el CRT estático (/MT): no depende de .NET ni del runtime de VC++, así que sirve también donde el .bat
 * sería la única opción (WinPE). Lo compila build_portable.py con las Build Tools de Visual Studio. */
#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#include <windows.h>
#include <wchar.h>

#define LARGO 4096

static void falla(const wchar_t *que, const wchar_t *ruta, DWORD err)
{
    wchar_t sis[512] = L"", msg[LARGO + 1024];
    if (err)
        FormatMessageW(FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, NULL, err, 0, sis, 512, NULL);
    _snwprintf(msg, sizeof msg / sizeof msg[0] - 1, L"%s\n\n%s\n\n%s\nSi no se resuelve, probá DeepSeekChat-consola.bat "
               L"o Diagnostico.bat de la misma carpeta.", que, ruta, sis);
    msg[sizeof msg / sizeof msg[0] - 1] = 0;
    MessageBoxW(NULL, msg, L"DeepSeek Chat", MB_OK | MB_ICONERROR);
}

int WINAPI wWinMain(HINSTANCE inst, HINSTANCE prev, PWSTR args, int show)
{
    wchar_t dir[LARGO], py[LARGO], app[LARGO], cmd[3 * LARGO];
    DWORD n = GetModuleFileNameW(NULL, dir, LARGO);
    if (n == 0 || n >= LARGO) {
        falla(L"No se pudo averiguar en qué carpeta está el programa.", L"", GetLastError());
        return 1;
    }
    wchar_t *barra = wcsrchr(dir, L'\\');
    if (barra) *barra = 0;

    _snwprintf(py, LARGO, L"%s\\runtime\\pythonw.exe", dir);
    _snwprintf(app, LARGO, L"%s\\app\\DeepSeekChat.pyw", dir);
    py[LARGO - 1] = app[LARGO - 1] = 0;
    if (GetFileAttributesW(py) == INVALID_FILE_ATTRIBUTES) {
        falla(L"Falta el Python propio de la carpeta portable (runtime\\). ¿Se copió la carpeta entera?", py, 0);
        return 1;
    }
    if (GetFileAttributesW(app) == INVALID_FILE_ATTRIBUTES) {
        falla(L"Falta la aplicación (app\\). ¿Se copió la carpeta entera?", app, 0);
        return 1;
    }

    _snwprintf(cmd, 3 * LARGO, L"\"%s\" -I \"%s\"", py, app);
    cmd[3 * LARGO - 1] = 0;
    STARTUPINFOW si = { sizeof si };
    PROCESS_INFORMATION pi;
    /* carpeta de trabajo = la del programa, igual que un doble clic sobre el .bat */
    if (!CreateProcessW(py, cmd, NULL, NULL, FALSE, 0, NULL, dir, &si, &pi)) {
        falla(L"No se pudo arrancar la aplicación.", py, GetLastError());
        return 1;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return 0;
}
